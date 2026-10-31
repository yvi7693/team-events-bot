"""Создание ивента (пошагово и одной строкой) и редактирование отдельных полей.

Мастер и редактор используют одни и те же шаги: в режиме редактирования (edit_id в данных FSM)
после шага изменение сразу сохраняется и снова показывается меню редактирования.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

from .. import logic
from ..models import Event, Kind, User
from ..quickparse import ParseError, parse_line
from ..render import card_header, event_summary
from ..service import Service
from ..timeutil import WEEKDAYS_SHORT, fmt_dt, fmt_duration, fmt_offset, from_ts, parse_duration, parse_time, parse_when, tz_label

router = Router(name="create")
router.message.filter(F.chat.type == "private")


class Wz(StatesGroup):
    title = State()
    kind = State()
    days = State()
    when = State()
    duration = State()
    format = State()
    participants = State()
    link = State()
    agenda = State()
    reminders = State()
    confirm = State()


class WzCb(CallbackData, prefix="wz"):
    a: str
    v: str = ""


STEPS = ["title", "kind", "days", "when", "duration", "format", "participants", "link", "agenda", "reminders"]
REMINDER_PRESETS = [1440, 180, 60, 30, 15, 10, 5]
KIND_LABELS = {
    "single": "• Разовый",
    "daily": "🔁 Дейли (пн–пт)",
    "weekly": "🔁 Еженедельно",
    "custom": "🔁 Свои дни / раз в N недель",
}


def cb(a: str, v: str = "") -> str:
    return WzCb(a=a, v=v).pack()


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


# ---------- черновик <-> ивент ----------

def draft_to_event(d: dict, organizer: User, ev: Event | None = None) -> Event:
    tz = ev.tz if ev else organizer.tz
    start = datetime.fromisoformat(d["start"]).astimezone(ZoneInfo(tz))
    recurring = d["kind"] != "single"
    weekdays = d.get("weekdays") or []
    if d["kind"] == "daily":
        weekdays = [0, 1, 2, 3, 4]
    elif d["kind"] == "weekly":
        weekdays = [start.weekday()]
    new = ev or Event(id=0, title="", kind=Kind.RECURRING if recurring else Kind.SINGLE, tz=tz,
                      start_date="", start_time="", duration_min=30, organizer_id=organizer.id)
    new.title = d["title"]
    new.start_date, new.start_time = start.date().isoformat(), start.strftime("%H:%M")
    new.duration_min = d["duration"]
    new.weekdays = sorted(weekdays) if recurring else []
    new.interval_weeks = d.get("interval", 1) if recurring else 1
    new.call_url = d.get("url")
    new.agenda = d.get("agenda")
    new.reminders = sorted(d.get("reminders", []), reverse=True)
    new.participant_ids = [] if recurring else d.get("participants", [])
    new.online_only = bool(d.get("online"))
    return new


def event_to_draft(ev: Event, start_ts: int) -> dict:
    kind = "single"
    if ev.is_recurring:
        if sorted(ev.weekdays) == [0, 1, 2, 3, 4] and ev.interval_weeks == 1:
            kind = "daily"
        elif len(ev.weekdays) == 1 and ev.interval_weeks == 1:
            kind = "weekly"
        else:
            kind = "custom"
    return {
        "title": ev.title, "kind": kind, "weekdays": ev.weekdays, "interval": ev.interval_weeks,
        "start": from_ts(start_ts, ev.tz).isoformat(), "duration": ev.duration_min,
        "participants": ev.participant_ids, "url": ev.call_url, "agenda": ev.agenda,
        "reminders": ev.reminders, "online": ev.online_only,
    }


# ---------- вход ----------

async def start_wizard(chat_id: int, user: User, state: FSMContext, svc: Service) -> None:
    await state.clear()
    await state.update_data(organizer=user.id, participants=[user.id])
    await ask(chat_id, "title", state, svc)


@router.message(Command("new"))
async def new_cmd(message: Message, command: CommandObject, state: FSMContext, svc: Service) -> None:
    user = await svc.db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала нажми /start — выберем часовой пояс.")
        return
    if not command.args:
        await start_wizard(message.chat.id, user, state, svc)
        return
    try:
        p = parse_line(command.args, user.tz)
    except ParseError as e:
        await message.answer(f"❗ {escape(str(e))}\n\nИли просто /new без параметров — спрошу по шагам.")
        return
    team = await svc.db.team()
    by_username = {u.username.lower(): u.id for u in team if u.username}
    unknown = [n for n in p.usernames if n not in by_username]
    participants = [user.id] + [by_username[n] for n in p.usernames if n in by_username]
    if p.everyone:
        participants = [u.id for u in team]
    if p.recurring:
        kind = "daily" if p.weekdays == [0, 1, 2, 3, 4] and p.interval_weeks == 1 else (
            "weekly" if len(p.weekdays) == 1 and p.interval_weeks == 1 else "custom")
    else:
        kind = "single"
    await state.clear()
    await state.update_data(
        organizer=user.id, title=p.title, kind=kind, weekdays=p.weekdays, interval=p.interval_weeks,
        start=p.start.isoformat(), duration=p.duration_min, participants=list(dict.fromkeys(participants)),
        url=p.call_url, agenda=p.agenda, online=p.online,
        reminders=logic.default_reminders(Kind.SINGLE if kind == "single" else Kind.RECURRING, p.weekdays),
    )
    if unknown:
        await message.answer(
            "Не нашёл в команде: " + ", ".join("@" + escape(n) for n in unknown)
            + ". Им нужно нажать /start у бота."
        )
    if kind == "single" and not p.usernames and not p.everyone:
        await ask(message.chat.id, "participants", state, svc)
    else:
        await show_preview(message.chat.id, state, svc)


# ---------- шаги ----------

async def ask(chat_id: int, step: str, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    user = await svc.db.get_user(d.get("organizer") or chat_id)
    send = svc.bot.send_message
    cancel_row = [Btn(text="✖️ Отмена", callback_data=cb("cancel"))]
    await state.set_state(getattr(Wz, step))

    if step == "title":
        await send(chat_id, "Как назовём ивент?", reply_markup=kb(cancel_row))
    elif step == "kind":
        await send(chat_id, "Какой это ивент?", reply_markup=kb(
            *[[Btn(text=label, callback_data=cb("kind", k))] for k, label in KIND_LABELS.items()], cancel_row))
    elif step == "days":
        await send(chat_id, "По каким дням и как часто?", reply_markup=days_kb(d))
    elif step == "when":
        tz = user.tz if user else await svc.team_tz()
        if d.get("kind") == "single":
            hint = "Когда начало? Например: <code>15:30</code>, <code>завтра 10:00</code>, <code>пт 11:00</code>, <code>05.11 14:00</code>"
        else:
            hint = ("Во сколько? Например <code>10:00</code> — начнём с ближайшего подходящего дня, "
                    "или <code>пн 10:00</code> / <code>10.11 10:00</code> — с конкретной даты")
        await send(chat_id, f"{hint}\n<i>Время — в твоём поясе ({tz_label(tz)})</i>", reply_markup=kb(cancel_row))
    elif step == "duration":
        await send(chat_id, "Сколько длится? Кнопкой или текстом: <code>20м</code>, <code>1ч30м</code>", reply_markup=kb(
            [Btn(text=fmt_duration(m), callback_data=cb("dur", str(m))) for m in (15, 30, 45)],
            [Btn(text=fmt_duration(m), callback_data=cb("dur", str(m))) for m in (60, 90, 120)],
            cancel_row))
    elif step == "format":
        await send(chat_id, "Где проходит?", reply_markup=kb(
            [Btn(text="🏢 Офис + онлайн", callback_data=cb("fmt", "hybrid"))],
            [Btn(text="💻 Только онлайн", callback_data=cb("fmt", "online"))],
            cancel_row))
    elif step == "participants":
        await send(chat_id, "Кто участвует? Отметь людей и нажми «Готово».\n"
                            "<i>Нет нужного человека — ему нужно нажать /start у бота.</i>",
                   reply_markup=participants_kb(d, await svc.db.team()))
    elif step == "link":
        hint = ("Ссылка на звонок?" if d.get("online")
                else "Ссылка на звонок? Она появится в карточке, если кто-то будет онлайн.")
        await send(chat_id, hint,
                   reply_markup=kb([Btn(text="Без ссылки", callback_data=cb("link", "-"))], cancel_row))
    elif step == "agenda":
        await send(chat_id, "Повестка? (можно несколько строк)",
                   reply_markup=kb([Btn(text="Без повестки", callback_data=cb("agenda", "-"))], cancel_row))
    elif step == "reminders":
        if "reminders" not in d:
            kind = Kind.SINGLE if d.get("kind") == "single" else Kind.RECURRING
            weekdays = [0, 1, 2, 3, 4] if d.get("kind") == "daily" else d.get("weekdays", [])
            if d.get("kind") == "weekly":
                weekdays = [0]
            await state.update_data(reminders=logic.default_reminders(kind, weekdays))
            d = await state.get_data()
        await send(chat_id, "Когда напоминать? Плюс я всегда пишу в момент начала.", reply_markup=reminders_kb(d))


def days_kb(d: dict) -> InlineKeyboardMarkup:
    days = set(d.get("weekdays", []))
    interval = d.get("interval", 1)
    row = [Btn(text=("✅" if i in days else "") + WEEKDAYS_SHORT[i], callback_data=cb("day", str(i))) for i in range(7)]
    intervals = [Btn(text=("✅ " if interval == n else "") + label, callback_data=cb("int", str(n)))
                 for n, label in ((1, "каждую нед."), (2, "раз в 2"), (3, "раз в 3"), (4, "раз в 4"))]
    return kb(row[:4], row[4:], intervals[:2], intervals[2:], [Btn(text="Готово ➡️", callback_data=cb("days_ok"))])


def participants_kb(d: dict, team: list[User]) -> InlineKeyboardMarkup:
    chosen = set(d.get("participants", []))
    rows = [[Btn(text=("✅ " if u.id in chosen else "▫️ ") + u.name, callback_data=cb("p", str(u.id)))] for u in team]
    rows.append([Btn(text="👥 Вся команда", callback_data=cb("p", "all")),
                 Btn(text="Готово ➡️", callback_data=cb("p_ok"))])
    return kb(*rows)


def reminders_kb(d: dict) -> InlineKeyboardMarkup:
    chosen = set(d.get("reminders", []))
    btns = [Btn(text=("✅ " if m in chosen else "▫️ ") + fmt_offset(m), callback_data=cb("rem", str(m)))
            for m in REMINDER_PRESETS]
    return kb(btns[:3], btns[3:6], btns[6:], [Btn(text="Готово ➡️", callback_data=cb("rem_ok"))])


async def advance(chat_id: int, current: str, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    if d.get("edit_id"):
        from .events import apply_edit
        await apply_edit(chat_id, current, state, svc)
        return
    for step in STEPS[STEPS.index(current) + 1:]:
        if step == "days" and d.get("kind") != "custom":
            continue
        if step == "participants" and d.get("kind") != "single":
            continue
        await ask(chat_id, step, state, svc)
        return
    await show_preview(chat_id, state, svc)


# ---------- обработчики шагов ----------

@router.callback_query(WzCb.filter(F.a == "cancel"))
async def wz_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.message.edit_text("Отменено.")
    await cq.answer()


@router.message(Wz.title, F.text)
async def on_title(message: Message, state: FSMContext, svc: Service) -> None:
    await state.update_data(title=message.text.strip()[:120])
    await advance(message.chat.id, "title", state, svc)


@router.callback_query(Wz.kind, WzCb.filter(F.a == "kind"))
async def on_kind(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    kind = callback_data.v
    upd: dict = {"kind": kind}
    if kind == "daily":
        upd.update(weekdays=[0, 1, 2, 3, 4], interval=1)
    elif kind == "weekly":
        upd.update(interval=1)
    await state.update_data(**upd)
    await cq.message.edit_text(f"Тип: {KIND_LABELS[kind]}")
    await cq.answer()
    await advance(cq.message.chat.id, "kind", state, svc)


@router.callback_query(Wz.days, WzCb.filter(F.a.in_({"day", "int", "days_ok"})))
async def on_days(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    if callback_data.a == "day":
        days = set(d.get("weekdays", []))
        days ^= {int(callback_data.v)}
        await state.update_data(weekdays=sorted(days))
    elif callback_data.a == "int":
        await state.update_data(interval=int(callback_data.v))
    else:
        if not d.get("weekdays"):
            await cq.answer("Выбери хотя бы один день", show_alert=True)
            return
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer()
        await advance(cq.message.chat.id, "days", state, svc)
        return
    await cq.message.edit_reply_markup(reply_markup=days_kb(await state.get_data()))
    await cq.answer()


def resolve_start(text: str, d: dict, tz: str, now: datetime | None = None) -> datetime | None:
    zone = ZoneInfo(tz)
    now = now or datetime.now(zone)
    weekdays = [0, 1, 2, 3, 4] if d.get("kind") == "daily" else d.get("weekdays") or []
    if d.get("kind") in ("daily", "custom") and (t := parse_time(text)):
        start = None
        for i in range(8):
            cand = datetime.combine(now.date() + timedelta(days=i), t, tzinfo=zone)
            if cand > now and cand.weekday() in weekdays:
                start = cand
                break
        return start
    start = parse_when(text, tz, now)
    if start and d.get("kind") in ("daily", "custom") and start.weekday() not in weekdays:
        # дата вне дней повтора — сдвигаем на ближайший подходящий день
        for i in range(1, 8):
            cand = start + timedelta(days=i)
            if cand.weekday() in weekdays:
                return cand
    return start


@router.message(Wz.when, F.text)
async def on_when(message: Message, state: FSMContext, svc: Service) -> None:
    user = await svc.db.get_user(message.from_user.id)
    d = await state.get_data()
    start = resolve_start(message.text, d, user.tz)
    if not start:
        await message.answer("Не понял время 🤔 Примеры: <code>15:30</code>, <code>завтра 10:00</code>, <code>пт 11:00</code>, <code>05.11 14:00</code>")
        return
    if d.get("kind") == "single" and start <= datetime.now(start.tzinfo):
        await message.answer("Это время уже прошло — укажи будущее.")
        return
    await state.update_data(start=start.isoformat())
    await message.answer(f"Начало: <b>{fmt_dt(int(start.timestamp()), user.tz)}</b>")
    await advance(message.chat.id, "when", state, svc)


@router.callback_query(Wz.duration, WzCb.filter(F.a == "dur"))
async def on_duration_btn(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    await state.update_data(duration=int(callback_data.v))
    await cq.message.edit_text(f"Длительность: {fmt_duration(int(callback_data.v))}")
    await cq.answer()
    await advance(cq.message.chat.id, "duration", state, svc)


@router.message(Wz.duration, F.text)
async def on_duration_text(message: Message, state: FSMContext, svc: Service) -> None:
    dur = parse_duration(message.text)
    if not dur or dur > 24 * 60:
        await message.answer("Не понял длительность. Примеры: <code>20</code>, <code>45м</code>, <code>1ч30м</code>")
        return
    await state.update_data(duration=dur)
    await advance(message.chat.id, "duration", state, svc)


@router.callback_query(Wz.format, WzCb.filter(F.a == "fmt"))
async def on_format(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    online = callback_data.v == "online"
    await state.update_data(online=online)
    await cq.message.edit_text("Формат: " + ("💻 только онлайн" if online else "🏢 офис + онлайн"))
    await cq.answer()
    await advance(cq.message.chat.id, "format", state, svc)


@router.callback_query(Wz.participants, WzCb.filter(F.a.in_({"p", "p_ok"})))
async def on_participants(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    team = await svc.db.team()
    chosen = list(d.get("participants", []))
    if callback_data.a == "p_ok":
        if not chosen:
            await cq.answer("Выбери хотя бы одного участника", show_alert=True)
            return
        names = ", ".join(u.name for u in team if u.id in chosen)
        await cq.message.edit_text(f"Участники: {escape(names)}")
        await cq.answer()
        await advance(cq.message.chat.id, "participants", state, svc)
        return
    if callback_data.v == "all":
        chosen = [] if len(chosen) == len(team) else [u.id for u in team]
    else:
        uid = int(callback_data.v)
        chosen = [x for x in chosen if x != uid] if uid in chosen else chosen + [uid]
    await state.update_data(participants=chosen)
    await cq.message.edit_reply_markup(reply_markup=participants_kb(await state.get_data(), team))
    await cq.answer()


@router.callback_query(Wz.link, WzCb.filter(F.a == "link"))
async def on_no_link(cq: CallbackQuery, state: FSMContext, svc: Service) -> None:
    await state.update_data(url=None)
    await cq.message.edit_text("Без ссылки.")
    await cq.answer()
    await advance(cq.message.chat.id, "link", state, svc)


@router.message(Wz.link, F.text)
async def on_link(message: Message, state: FSMContext, svc: Service) -> None:
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        if "." in url and " " not in url:
            url = "https://" + url
        else:
            await message.answer("Это не похоже на ссылку. Пришли URL или нажми «Без ссылки».")
            return
    await state.update_data(url=url)
    await advance(message.chat.id, "link", state, svc)


@router.callback_query(Wz.agenda, WzCb.filter(F.a == "agenda"))
async def on_no_agenda(cq: CallbackQuery, state: FSMContext, svc: Service) -> None:
    await state.update_data(agenda=None)
    await cq.message.edit_text("Без повестки.")
    await cq.answer()
    await advance(cq.message.chat.id, "agenda", state, svc)


@router.message(Wz.agenda, F.text)
async def on_agenda(message: Message, state: FSMContext, svc: Service) -> None:
    await state.update_data(agenda=message.text.strip()[:1500])
    await advance(message.chat.id, "agenda", state, svc)


@router.callback_query(Wz.reminders, WzCb.filter(F.a.in_({"rem", "rem_ok"})))
async def on_reminders(cq: CallbackQuery, callback_data: WzCb, state: FSMContext, svc: Service) -> None:
    if callback_data.a == "rem_ok":
        d = await state.get_data()
        rem = sorted(d.get("reminders", []), reverse=True)
        text = ", ".join(fmt_offset(m) for m in rem)
        await cq.message.edit_text(f"Напоминания: {text + ' и ' if text else ''}в момент начала")
        await cq.answer()
        await advance(cq.message.chat.id, "reminders", state, svc)
        return
    d = await state.get_data()
    rem = set(d.get("reminders", []))
    rem ^= {int(callback_data.v)}
    await state.update_data(reminders=sorted(rem, reverse=True))
    await cq.message.edit_reply_markup(reply_markup=reminders_kb(await state.get_data()))
    await cq.answer()


# ---------- превью, конфликты, сохранение ----------

async def show_preview(chat_id: int, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    organizer = await svc.db.get_user(d["organizer"])
    ev = draft_to_event(d, organizer)
    team = await svc.db.team()
    people = [u for u in team if u.id in set(logic.participant_ids(ev, team))]
    occs = svc.candidate_occurrences(ev)
    text = "<b>Проверь ивент</b>\n\n" + event_summary(ev, organizer.tz, occs[0] if occs else None, people)
    conflicts = await svc.conflicts(ev, occs, {u.id for u in people})
    if conflicts:
        text += "\n\n" + await svc.conflicts_text(conflicts, organizer.tz)
    await state.set_state(Wz.confirm)
    save_label = "⚠️ Создать всё равно" if conflicts else "✅ Создать"
    await svc.bot.send_message(chat_id, text, disable_web_page_preview=True, reply_markup=kb(
        [Btn(text=save_label, callback_data=cb("save"))],
        [Btn(text="✖️ Отмена", callback_data=cb("cancel"))],
    ))


@router.callback_query(Wz.confirm, WzCb.filter(F.a == "save"))
async def on_save(cq: CallbackQuery, state: FSMContext, svc: Service) -> None:
    d = await state.get_data()
    organizer = await svc.db.get_user(d["organizer"])
    ev = await svc.db.create_event(draft_to_event(d, organizer))
    await state.clear()
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer("Создано!")

    occs = svc.candidate_occurrences(ev)
    occ = occs[0] if occs else None
    header = card_header("🆕", "новый ивент")
    if not occ:
        await svc.safe_send(cq.message.chat.id, "Создано, но ближайших вхождений нет.")
        return
    # анонс — только в общий чат с отметками участников
    if chat := await svc.db.team_chat_id():
        await svc.send_card(chat, occ, header)
        await svc.safe_send(cq.message.chat.id, "✅ Ивент создан. Напоминания будут приходить в общий чат.")
    else:
        await svc.safe_send(cq.message.chat.id, "✅ Ивент создан, но общий чат не подключён — напоминаниям некуда "
                                                "приходить. Добавь бота в чат команды.")
