"""Карточки ивентов: RSVP, перенос, редактирование, отмена/удаление; /events и /week."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

from .. import logic
from ..models import Event, Occurrence, Rsvp, User
from ..recurrence import base_start, describe
from ..render import EvCb, RsvpCb, WeekCb, event_summary, week_keyboard, week_text
from ..service import Service
from ..timeutil import MONTHS_GEN, fmt_dt, fmt_duration, fmt_offset, from_ts, now_ts, parse_time, parse_when, week_bounds
from .create import ask, draft_to_event, event_to_draft

router = Router(name="events")
router.message.filter(F.chat.type == "private")

NOT_REGISTERED = "Сначала нажми «Старт» у бота в личке — так я смогу присылать тебе напоминания."


class MvCb(CallbackData, prefix="mv"):
    a: str          # scope | delta | manual | go
    e: int
    k: int
    s: str = ""     # one | series
    t: int = 0      # секунды сдвига (delta) или новый timestamp (go)


class EdCb(CallbackData, prefix="ed"):
    f: str
    e: int
    k: int


class Mv(StatesGroup):
    when = State()


async def _load(cq: CallbackQuery, svc: Service, eid: int, key: int) -> tuple[User, Event, Occurrence] | None:
    user = await svc.db.get_user(cq.from_user.id)
    if not user:
        await cq.answer(NOT_REGISTERED, show_alert=True)
        return None
    occ = await svc.db.occurrence(eid, key)
    if not occ:
        await cq.answer("Этого ивента больше нет или он отменён.", show_alert=True)
        return None
    return user, occ.event, occ


async def _to_private(cq: CallbackQuery) -> int:
    """Диалоги ведём в личке; если нажали в группе — сообщаем, куда смотреть."""
    if cq.message.chat.type != "private":
        await cq.answer("Написал тебе в личку 👌")
    else:
        await cq.answer()
    return cq.from_user.id


# ---------- RSVP ----------

@router.callback_query(RsvpCb.filter())
async def on_rsvp(cq: CallbackQuery, callback_data: RsvpCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    if user.id not in {u.id for u in await svc.people(ev)}:
        await cq.answer("Ты не участник этого ивента. Попроси организатора добавить тебя.", show_alert=True)
        return
    if callback_data.s != Rsvp.LATE.value:  # старые карточки с «Буду / Не буду»
        await cq.answer("Эта кнопка больше не используется — осталась только «Опоздаю».", show_alert=True)
        return
    current = (await svc.db.rsvps(ev.id, occ.key)).get(user.id)
    answer = logic.toggle_late(current)
    await svc.db.set_answer(ev.id, occ.key, user.id, answer)
    late = answer.status == Rsvp.LATE
    await cq.answer("Записал: опоздаю" if late else "Отменил опоздание")  # сначала снимаем «часики»
    svc.refresh_cards_soon(ev.id, occ.key)  # строка «Опоздают: …» в карточках


# ---------- перенос ----------

@router.callback_query(EvCb.filter(F.a == "move"))
async def on_move(cq: CallbackQuery, callback_data: EvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    if not logic.can_edit(ev, user.id, await svc.db.team()):
        await cq.answer("Переносить могут только участники ивента.", show_alert=True)
        return
    await start_move(await _to_private(cq), user, ev, occ, svc)


async def start_move(chat: int, user: User, ev: Event, occ: Occurrence, svc: Service) -> None:
    if ev.is_recurring:
        await svc.safe_send(chat, f"Перенести «{escape(ev.title)}» ({fmt_dt(occ.start, user.tz)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="Только этот раз", callback_data=MvCb(a="scope", e=ev.id, k=occ.key, s="one").pack())],
            [Btn(text="Всю серию", callback_data=MvCb(a="scope", e=ev.id, k=occ.key, s="series").pack())],
        ]))
    else:
        await _move_options(chat, ev, occ, "one", user, svc)


@router.callback_query(MvCb.filter(F.a == "scope"))
async def on_move_scope(cq: CallbackQuery, callback_data: MvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer()
    await _move_options(cq.message.chat.id, ev, occ, callback_data.s, user, svc)


async def _move_options(chat: int, ev: Event, occ: Occurrence, scope: str, user: User, svc: Service) -> None:
    def delta(label: str, sec: int) -> Btn:
        return Btn(text=label, callback_data=MvCb(a="delta", e=ev.id, k=occ.key, s=scope, t=sec).pack())

    rows = [[delta("+15 мин", 900), delta("+30 мин", 1800), delta("+1 ч", 3600)],
            [delta("−15 мин", -900), delta("−30 мин", -1800)]]
    if scope == "one":
        rows.append([delta("На завтра, то же время", 86400)])
    rows.append([Btn(text="✍️ Указать время", callback_data=MvCb(a="manual", e=ev.id, k=occ.key, s=scope).pack())])
    what = "всю серию" if scope == "series" else "этот раз"
    await svc.safe_send(chat, f"Сейчас: <b>{fmt_dt(occ.start, user.tz)}</b>. Куда сдвинуть {what}?",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(MvCb.filter(F.a == "delta"))
async def on_move_delta(cq: CallbackQuery, callback_data: MvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer()
    await _confirm_move(cq.message.chat.id, user, ev, occ, callback_data.s, occ.start + callback_data.t, svc)


@router.callback_query(MvCb.filter(F.a == "manual"))
async def on_move_manual(cq: CallbackQuery, callback_data: MvCb, state: FSMContext, svc: Service) -> None:
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer()
    await state.set_state(Mv.when)
    await state.update_data(mv_e=callback_data.e, mv_k=callback_data.k, mv_s=callback_data.s)
    if callback_data.s == "series":
        hint = "Новое время для всей серии, например <code>10:30</code>"
    else:
        hint = "Новое время: <code>15:30</code>, <code>завтра 10:00</code>, <code>пт 11:00</code>, <code>05.11 14:00</code>"
    await svc.safe_send(cq.message.chat.id, hint + "\n/cancel — отмена")


@router.message(Mv.when, F.text)
async def on_move_text(message: Message, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    user = await svc.db.get_user(message.from_user.id)
    occ = await svc.db.occurrence(d["mv_e"], d["mv_k"])
    if not occ or not user:
        await state.clear()
        await message.answer("Ивент не найден.")
        return
    if d["mv_s"] == "series":
        t = parse_time(message.text)
        if not t:
            await message.answer("Нужно время в формате ЧЧ:ММ, например 10:30")
            return
        # время введено в поясе пользователя — привязываем к дате этого вхождения
        local_day = from_ts(occ.start, user.tz)
        new_start = int(local_day.replace(hour=t.hour, minute=t.minute).timestamp())
    else:
        dt = parse_when(message.text, user.tz)
        if not dt:
            await message.answer("Не понял время. Примеры: 15:30, завтра 10:00, пт 11:00, 05.11 14:00")
            return
        new_start = int(dt.timestamp())
    await state.clear()
    await _confirm_move(message.chat.id, user, occ.event, occ, d["mv_s"], new_start, svc)


async def _confirm_move(chat: int, user: User, ev: Event, occ: Occurrence, scope: str, new_start: int, svc: Service) -> None:
    if new_start + ev.duration_min * 60 <= now_ts():
        await svc.safe_send(chat, "Это время уже прошло — выбери другое.")
        return
    if ev.is_recurring and scope == "series":
        shift = new_start - occ.start
        candidate = [Occurrence(ev, o.key, o.start + shift, o.end + shift) for o in svc.candidate_occurrences(ev)]
    else:
        candidate = [Occurrence(ev, occ.key, new_start, new_start + ev.duration_min * 60)]
    conflicts = await svc.conflicts(ev, candidate, {u.id for u in await svc.people(ev)})
    if conflicts:
        text = f"Перенос на <b>{fmt_dt(new_start, user.tz)}</b>\n\n" + await svc.conflicts_text(conflicts, user.tz)
        await svc.safe_send(chat, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="⚠️ Перенести всё равно", callback_data=MvCb(a="go", e=ev.id, k=occ.key, s=scope, t=new_start).pack())],
            [Btn(text="✖️ Отмена", callback_data=EvCb(a="noop", e=ev.id).pack())],
        ]))
        return
    await svc.move(ev, occ, new_start, scope, user)
    await svc.safe_send(chat, f"✅ Перенесено на <b>{fmt_dt(new_start, user.tz)}</b>. Участники получили уведомление.")


@router.callback_query(MvCb.filter(F.a == "go"))
async def on_move_go(cq: CallbackQuery, callback_data: MvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer()
    await svc.move(ev, occ, callback_data.t, callback_data.s, user)
    await svc.safe_send(cq.message.chat.id, f"✅ Перенесено на <b>{fmt_dt(callback_data.t, user.tz)}</b>.")


@router.callback_query(EvCb.filter(F.a == "noop"))
async def on_noop(cq: CallbackQuery) -> None:
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer("Отменено")


# ---------- редактирование ----------

def edit_menu(ev: Event, key: int, user: User) -> InlineKeyboardMarkup:
    def b(text: str, f: str) -> Btn:
        return Btn(text=text, callback_data=EdCb(f=f, e=ev.id, k=key).pack())

    rows = [[b("Название", "title"), b("Длительность", "duration")],
            [b("Ссылка", "link"), b("Повестка", "agenda")],
            [b("Напоминания", "reminders"), b("Формат", "format")],
            [b("🕑 Перенести", "move")]]
    rows.append([b("Дни повтора", "days")] if ev.is_recurring else [b("Участники", "participants")])
    if logic.can_delete(ev, user.id):
        if ev.is_recurring:
            rows.append([b("🚫 Отменить этот раз", "cancel")])
        rows.append([b("🗑 Удалить ивент", "delete")])
    rows.append([b("Готово", "done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_edit_menu(chat: int, ev: Event, occ: Occurrence, user: User, svc: Service) -> None:
    people = await svc.people(ev)
    text = "✏️ <b>Что изменить?</b>\n\n" + event_summary(ev, user.tz, occ, people)
    await svc.safe_send(chat, text, reply_markup=edit_menu(ev, occ.key, user))


@router.callback_query(EvCb.filter(F.a == "edit"))
async def on_edit(cq: CallbackQuery, callback_data: EvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    if not logic.can_edit(ev, user.id, await svc.db.team()):
        await cq.answer("Редактировать могут только участники ивента.", show_alert=True)
        return
    await send_edit_menu(await _to_private(cq), ev, occ, user, svc)


@router.callback_query(EdCb.filter())
async def on_edit_field(cq: CallbackQuery, callback_data: EdCb, state: FSMContext, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    f = callback_data.f
    chat = cq.message.chat.id
    if f == "done":
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer("Готово")
        return
    await cq.answer()
    if f == "move":
        await start_move(chat, user, ev, occ, svc)
        return
    if f in ("cancel", "delete"):
        what = f"отменить «{escape(ev.title)}» {fmt_dt(occ.start, user.tz)}" if f == "cancel" else f"удалить «{escape(ev.title)}» насовсем"
        await svc.safe_send(chat, f"Точно {what}? Участники получат уведомление.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            Btn(text="Да", callback_data=EvCb(a=f, e=ev.id, k=occ.key).pack()),
            Btn(text="Нет", callback_data=EvCb(a="noop", e=ev.id).pack()),
        ]]))
        return
    draft = event_to_draft(ev, base_start(ev))
    if f == "days":
        draft["kind"] = "custom"
    await state.clear()
    await state.update_data(**draft, organizer=ev.organizer_id, edit_id=ev.id, edit_key=occ.key, editor=user.id)
    await ask(chat, f, state, svc)


FIELD_NAMES = {
    "title": "название", "duration": "длительность", "days": "дни повтора", "participants": "участники",
    "link": "ссылка на звонок", "agenda": "повестка", "reminders": "напоминания", "format": "формат",
}


async def apply_edit(chat: int, field: str, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    await state.clear()
    ev = await svc.db.get_event(d["edit_id"])
    editor = await svc.db.get_user(d["editor"])
    if not ev or not editor:
        await svc.safe_send(chat, "Ивент не найден.")
        return
    organizer = await svc.db.get_user(ev.organizer_id) or editor
    before = await svc.people(ev)
    old_title = ev.title
    # дата/время в черновике не меняются — draft_to_event вернёт их как были
    keep_start = (ev.start_date, ev.start_time)
    ev = draft_to_event(d, organizer, ev)
    ev.start_date, ev.start_time = keep_start
    await svc.db.save_event(ev)

    after = await svc.people(ev)
    if field in ("duration", "days", "participants"):
        conflicts = await svc.conflicts(ev, svc.candidate_occurrences(ev), {u.id for u in after})
        if conflicts:
            await svc.safe_send(chat, "Сохранено, но есть пересечения.\n\n" + await svc.conflicts_text(conflicts, editor.tz))

    value = {
        "title": escape(ev.title),
        "duration": fmt_duration(ev.duration_min),
        "days": describe(ev),
        "participants": ", ".join(escape(u.name) for u in after),
        "link": escape(ev.call_url or "—"),
        "agenda": escape(ev.agenda or "—"),
        "reminders": ", ".join(fmt_offset(m) for m in ev.reminders) or "только в момент начала",
        "format": "только онлайн" if ev.online_only else "офис + онлайн",
    }[field]
    if field != "reminders":  # настройка напоминаний никого кроме бота не касается
        await svc.announce(
            ev,
            lambda tz: f"✏️ «{escape(old_title)}»: {FIELD_NAMES[field]} → {value}\nИнициатор: {escape(editor.name)}",
            extra_user_ids={u.id for u in before},
        )
    await svc.refresh_cards(ev.id)
    occ = await svc.db.occurrence(ev.id, d["edit_key"]) or await svc.db.next_occurrence(ev, now_ts())
    if occ:
        await send_edit_menu(chat, ev, occ, editor, svc)


@router.callback_query(EvCb.filter(F.a == "cancel"))
async def on_cancel_occ(cq: CallbackQuery, callback_data: EvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, occ = loaded
    if not logic.can_delete(ev, user.id):
        await cq.answer("Отменить может только организатор.", show_alert=True)
        return
    await svc.db.set_override(ev.id, occ.key, None, cancelled=True)
    await cq.message.edit_text(f"🚫 Отменено: «{escape(ev.title)}» {fmt_dt(occ.start, user.tz)}")
    await cq.answer()
    await svc.announce(ev, lambda tz: f"🚫 <b>Отменён</b> «{escape(ev.title)}» {fmt_dt(occ.start, tz)}\nИнициатор: {escape(user.name)}")
    await svc.refresh_cards(ev.id, occ.key)


@router.callback_query(EvCb.filter(F.a == "delete"))
async def on_delete(cq: CallbackQuery, callback_data: EvCb, svc: Service) -> None:
    loaded = await _load(cq, svc, callback_data.e, callback_data.k)
    if not loaded:
        return
    user, ev, _ = loaded
    if not logic.can_delete(ev, user.id):
        await cq.answer("Удалить может только организатор.", show_alert=True)
        return
    people = await svc.people(ev)
    await svc.db.delete_event(ev.id)
    await cq.message.edit_text(f"🗑 Удалено: «{escape(ev.title)}»")
    await cq.answer()
    await svc.announce(ev, lambda tz: f"🗑 <b>Удалён ивент</b> «{escape(ev.title)}» ({describe(ev)})\nИнициатор: {escape(user.name)}",
                       extra_user_ids={u.id for u in people})
    await svc.refresh_cards(ev.id)


# ---------- /events ----------

@router.message(Command("events"))
async def events_cmd(message: Message, svc: Service) -> None:
    team = await svc.db.team()
    private = message.chat.type == "private"
    tz = await svc.viewer_tz(message.chat.id)
    now = now_ts()
    rows, lines = [], ["<b>Ивенты</b>" + (" (где ты участник)" if private else "")]
    for ev in await svc.db.events():
        if private and message.from_user.id not in logic.participant_ids(ev, team) and ev.organizer_id != message.from_user.id:
            continue
        occ = await svc.db.next_occurrence(ev, now - ev.duration_min * 60)
        if not occ:
            continue
        mark = "🔁" if ev.is_recurring else "•"
        lines.append(f"{mark} <b>{escape(ev.title)}</b> — {describe(ev)}, ближайший {fmt_dt(occ.start, tz)}")
        rows.append([Btn(text=f"{mark} {ev.title}", callback_data=EvCb(a="open", e=ev.id, k=occ.key).pack())])
    if not rows:
        await message.answer("Ивентов пока нет. /new — создать.")
        return
    await message.answer("\n".join(lines) + "\n\nНажми на ивент, чтобы открыть карточку:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(EvCb.filter(F.a == "open"))
async def on_open(cq: CallbackQuery, callback_data: EvCb, svc: Service) -> None:
    occ = await svc.db.occurrence(callback_data.e, callback_data.k)
    if not occ:
        ev = await svc.db.get_event(callback_data.e)
        occ = await svc.db.next_occurrence(ev, now_ts()) if ev else None
    if not occ:
        await cq.answer("Ивент не найден", show_alert=True)
        return
    await cq.answer()
    await svc.send_card(cq.message.chat.id, occ)


# ---------- /week ----------

async def render_week(chat_id: int, user_id: int, offset: int, svc: Service) -> str:
    private = chat_id > 0
    tz = await svc.viewer_tz(chat_id)
    frm, to = week_bounds(now_ts(), tz, offset)
    team = await svc.db.team()
    events = await svc.db.events()
    if private:
        events = [e for e in events if user_id in logic.participant_ids(e, team) or e.organizer_id == user_id]
    occs = await svc.db.occurrences(frm, to, events)
    people_of = {e.id: [u for u in team if u.id in set(logic.participant_ids(e, team))] for e in events}
    my = None
    if private:
        my = {}
        for o in occs:
            if a := (await svc.db.rsvps(o.event.id, o.key)).get(user_id):
                my[(o.event.id, o.key)] = a
    a, b = from_ts(frm, tz), from_ts(to - 1, tz)
    if a.month == b.month:
        span = f"{a.day}–{b.day} {MONTHS_GEN[b.month - 1]}"
    else:
        span = f"{a.day} {MONTHS_GEN[a.month - 1]} – {b.day} {MONTHS_GEN[b.month - 1]}"
    title = ("Твои ивенты" if private else "Ивенты команды") + f": {span}"
    return week_text(title, occs, tz, people_of, len(team), my)


@router.message(Command("week"))
async def week_cmd(message: Message, svc: Service) -> None:
    text = await render_week(message.chat.id, message.from_user.id, 0, svc)
    await message.answer(text, reply_markup=week_keyboard(0), disable_web_page_preview=True)


@router.callback_query(WeekCb.filter())
async def week_nav(cq: CallbackQuery, callback_data: WeekCb, svc: Service) -> None:
    text = await render_week(cq.message.chat.id, cq.from_user.id, callback_data.o, svc)
    try:
        await cq.message.edit_text(text, reply_markup=week_keyboard(callback_data.o), disable_web_page_preview=True)
    except TelegramBadRequest:
        pass  # «message is not modified» при повторном нажатии
    await cq.answer()
