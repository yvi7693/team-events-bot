"""Операции поверх БД, которые рассылают сообщения: карточки, анонсы, переносы, конфликты."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from . import logic
from .db import Database
from .models import Event, Occurrence, Rsvp, User
from .recurrence import expand
from .render import card_keyboard, card_text, is_start, mentions
from .timeutil import fmt_dt, from_ts, now_ts

log = logging.getLogger(__name__)


class Service:
    def __init__(self, bot: Bot, db: Database, default_tz: str):
        self.bot = bot
        self.db = db
        self.default_tz = default_tz
        # фоновые перерисовки карточек: ключ -> «нужно ещё раз» (склейка частых нажатий)
        self._refreshing: dict[tuple[int, int | None], bool] = {}
        self._tasks: set[asyncio.Task] = set()

    async def team_tz(self) -> str:
        return await self.db.get_setting("team_tz") or self.default_tz

    async def people(self, ev: Event, team: list[User] | None = None) -> list[User]:
        team = team if team is not None else await self.db.team()
        ids = set(logic.participant_ids(ev, team))
        return [u for u in team if u.id in ids]

    async def viewer_tz(self, chat_id: int) -> str:
        if chat_id > 0 and (u := await self.db.get_user(chat_id)):
            return u.tz
        return await self.team_tz()

    # ---------- карточки ----------

    async def render(self, occ: Occurrence, chat_id: int, header: str = ""):
        team = await self.db.team()
        people = await self.people(occ.event, team)
        rsvps = await self.db.rsvps(occ.event.id, occ.key)
        other_tzs = sorted({u.tz for u in people} | {occ.event.tz})
        late = [u for u in people if (a := rsvps.get(u.id)) and a.status == Rsvp.LATE]
        # ссылка на звонок — текстом и только в сообщении о начале, когда кто-то онлайн
        url = occ.event.call_url if is_start(header) and logic.show_link(occ.event, people, rsvps) else None
        text = card_text(occ, await self.viewer_tz(chat_id), other_tzs, now_ts(), header, late, url)
        in_group = chat_id < 0
        if in_group:
            # отметки ставим в каждой карточке чата; при правке сообщения Telegram повторно не уведомляет
            text += "\n\n" + mentions(people, logic.whole_team(occ.event, team))
        kb = card_keyboard(occ, header, finished=occ.end <= now_ts(), manage=not in_group)
        return text, kb

    async def send_card(self, chat_id: int, occ: Occurrence, header: str = "") -> Message | None:
        text, kb = await self.render(occ, chat_id, header)
        msg = await self.safe_send(chat_id, text, reply_markup=kb)
        if msg:
            await self.db.add_card(occ.event.id, occ.key, chat_id, msg.message_id, header)
        return msg

    async def refresh_cards(self, event_id: int, key: int | None = None) -> None:
        """Перерисовывает все карточки вхождения сразу во всех чатах (параллельно)."""
        jobs = []
        for occ_key, chat_id, message_id, header in await self.db.cards(event_id, key):
            occ = await self.db.occurrence(event_id, occ_key)
            if occ is None:
                jobs.append(self._clear_card(chat_id, message_id))
            else:
                text, kb = await self.render(occ, chat_id, header)
                jobs.append(self._edit_card(chat_id, message_id, text, kb))
        await asyncio.gather(*jobs)

    async def _edit_card(self, chat_id: int, message_id: int, text: str, kb) -> None:
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=kb, disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.info("card %s/%s gone: %s", chat_id, message_id, e)
                await self.db.remove_card(chat_id, message_id)

    async def _clear_card(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramBadRequest:
            pass
        await self.db.remove_card(chat_id, message_id)

    def refresh_cards_soon(self, event_id: int, key: int | None = None) -> None:
        """Перерисовать карточки в фоне, не задерживая ответ пользователю.

        Если перерисовка уже идёт, просто отмечаем, что нужен ещё один проход —
        так серия быстрых нажатий даёт 1–2 перерисовки с последним состоянием.
        """
        k = (event_id, key)
        if k in self._refreshing:
            self._refreshing[k] = True
            return
        self._refreshing[k] = False

        async def run() -> None:
            try:
                while True:
                    await self.refresh_cards(event_id, key)
                    if not self._refreshing.get(k):
                        break
                    self._refreshing[k] = False
            except Exception:
                log.exception("background card refresh failed")
            finally:
                self._refreshing.pop(k, None)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_background(self) -> None:
        """Для тестов: дождаться фоновых перерисовок."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def safe_send(self, chat_id: int, text: str, **kw) -> Message | None:
        try:
            return await self.bot.send_message(chat_id, text, disable_web_page_preview=True, **kw)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("cannot send to %s: %s", chat_id, e)
            return None

    # ---------- анонсы ----------

    async def announce(
        self,
        ev: Event,
        make_text: Callable[[str], str],
        extra_user_ids: set[int] | None = None,
    ) -> None:
        """Уведомление об изменении — в общий чат с отметками участников. В личку бот сам не пишет.

        make_text(tz) — текст со временем в поясе чата; extra_user_ids — кого отметить дополнительно
        (например, убранных из участников).
        """
        chat = await self.db.team_chat_id()
        if not chat:
            log.warning("team chat is not bound, announcement for event %s dropped", ev.id)
            return
        team = await self.db.team()
        people = await self.people(ev, team)
        people += [u for u in team if u.id in (extra_user_ids or set()) and u not in people]
        text = make_text(await self.team_tz()) + "\n\n" + mentions(people, logic.whole_team(ev, team))
        await self.safe_send(chat, text)

    # ---------- конфликты ----------

    async def conflicts(self, ev: Event, candidate: list[Occurrence], participant_ids: set[int]) -> list[logic.Conflict]:
        if not candidate:
            return []
        frm = min(o.start for o in candidate)
        to = max(o.end for o in candidate)
        team = await self.db.team()
        others = []
        for other in await self.db.events():
            if other.id == ev.id:
                continue
            ids = set(logic.participant_ids(other, team))
            if not ids & participant_ids:
                continue
            for occ in expand(other, frm, to, await self.db.overrides(other.id)):
                others.append((occ, ids))
        return logic.find_conflicts(candidate, participant_ids, others)

    async def conflicts_text(self, conflicts: list[logic.Conflict], tz: str) -> str:
        lines = ["⚠️ <b>Пересечения по времени</b>"]
        for c in conflicts[:15]:
            u = await self.db.get_user(c.user_id)
            name = escape(u.name) if u else str(c.user_id)
            lines.append(f"• {name}: «{escape(c.other.event.title)}», {fmt_dt(c.other.start, tz)}")
        if len(conflicts) > 15:
            lines.append(f"…и ещё {len(conflicts) - 15}")
        return "\n".join(lines)

    def candidate_occurrences(self, ev: Event) -> list[Occurrence]:
        now = now_ts()
        if ev.is_recurring:
            return expand(ev, now, now + logic.CONFLICT_HORIZON_DAYS * 86400)
        return expand(ev, 0, 2**40)

    # ---------- перенос ----------

    async def move(self, ev: Event, occ: Occurrence, new_start: int, scope: str, actor: User) -> None:
        """scope: 'one' — только это вхождение, 'series' — вся серия (меняется время начала)."""
        old_start = occ.start
        if not ev.is_recurring:
            dt = from_ts(new_start, ev.tz)
            ev.start_date, ev.start_time = dt.date().isoformat(), dt.strftime("%H:%M")
            await self.db.save_event(ev)
            await self.db.move_occurrence_data(ev.id, occ.key, new_start)
            new_key = new_start
        elif scope == "one":
            await self.db.set_override(ev.id, occ.key, new_start)
            new_key = occ.key
        else:
            # серия: новое локальное время в поясе ивента, дни недели прежние
            local = from_ts(new_start, ev.tz)
            ev.start_time = local.strftime("%H:%M")
            await self.db.save_event(ev)
            await self.db.drop_future_overrides(ev.id, now_ts() - 86400)
            # ключ ближайшего вхождения сдвигается вместе со временем — переносим ответы и карточки
            ref = from_ts(occ.key, ev.tz)
            new_key = int(datetime.combine(ref.date(), local.time(), tzinfo=ref.tzinfo).timestamp())
            await self.db.move_occurrence_data(ev.id, occ.key, new_key)

        await self.refresh_cards(ev.id)
        what = "вся серия" if ev.is_recurring and scope == "series" else None
        title = escape(ev.title)

        def text(tz: str) -> str:
            s = f"🕑 <b>Перенос:</b> «{title}»\n{fmt_dt(old_start, tz)} → <b>{fmt_dt(new_start, tz)}</b>"
            if what:
                s += f"\n<i>{what}</i>"
            return s + f"\nИнициатор: {escape(actor.name)}"

        await self.announce(ev, text)
