"""Тексты карточек и клавиатуры."""

from __future__ import annotations

from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from .models import Answer, Event, Occurrence, Rsvp, User
from .recurrence import describe
from .timeutil import fmt_day, fmt_duration, fmt_offset, fmt_time, from_ts, now_ts, tz_label


class RsvpCb(CallbackData, prefix="r"):
    e: int
    k: int
    s: str


class EvCb(CallbackData, prefix="ev"):
    a: str        # open | edit | move | cancel | del
    e: int
    k: int = 0


class WeekCb(CallbackData, prefix="wk"):
    o: int


# короткая подпись: Telegram растягивает сообщение под самую длинную кнопку ряда
LATE_LABEL = "⏰ Опоздаю"


def card_header(icon: str, note: str = "") -> str:
    """Первая строка карточки: «⏰ test · через 5 мин». Хранится в БД как «иконка|пометка»."""
    return f"{icon}|{note}"


def reminder_header(offset_min: int) -> str:
    if offset_min == 0:
        return card_header("🚀", "начинается")
    return card_header("⏰", fmt_offset(offset_min).replace("за ", "через "))


def rel_day(ts: int, tz: str, now: int) -> str:
    """«сегодня», «завтра» или «пт, 6 ноября»."""
    days = (from_ts(ts, tz).date() - from_ts(now, tz).date()).days
    return {0: "сегодня", 1: "завтра"}.get(days, fmt_day(ts, tz))


def time_line(start: int, end: int, main_tz: str, other_tzs: list[str], now: int) -> str:
    """«сегодня, 12:10–12:25», а если у кого-то другой пояс — « · 13:10 Алматы»."""
    s = f"{rel_day(start, main_tz, now)}, {fmt_time(start, main_tz)}–{fmt_time(end, main_tz)}"
    extra = []
    for tz in other_tzs:
        if from_ts(start, tz).utcoffset() == from_ts(start, main_tz).utcoffset():
            continue
        label = f"{fmt_time(start, tz)} {tz_label(tz)}"
        if label not in extra:
            extra.append(label)
    return " · ".join([s, *extra])


def _names(users: list[User]) -> str:
    return ", ".join(escape(u.name) for u in users) or "—"


def is_start(header: str) -> bool:
    return header.startswith("🚀|")


def card_text(
    occ: Occurrence,
    viewer_tz: str,
    other_tzs: list[str],
    now: int,
    header: str = "",
    late: list[User] | None = None,
    call_url: str | None = None,
) -> str:
    """Карточка ивента — только главное:

        ⏰ test · через 5 мин
        сегодня, 12:10–12:25

        Повестка
        тест

    Ниже — только если есть: кто опаздывает и ссылка на звонок.
    """
    icon, note = header.split("|", 1) if "|" in header else ("📅", "")
    first = f"{icon} <b>{escape(occ.event.title)}</b>" + (f" · {note}" if note else "")
    lines = [first, time_line(occ.start, occ.end, viewer_tz, other_tzs, now)]
    if occ.event.agenda:
        lines += ["", "<b>Повестка</b>", escape(occ.event.agenda)]
    if late:
        lines += ["", f"⏰ {'Опоздает' if len(late) == 1 else 'Опоздают'}: {_names(late)}"]
    if call_url:
        lines += ["", f'🔗 <a href="{escape(call_url)}">Подключиться к звонку</a>']
    return "\n".join(lines)


def mentions(people: list[User], everyone: bool) -> str:
    """Отметки в общем чате. В Telegram нет настоящего @all, поэтому для всей команды
    видно «@all», а каждый участник отмечен невидимой ссылкой — уведомление получит каждый."""
    if everyone:
        return "📣 @all" + "".join(f'<a href="tg://user?id={u.id}">\u200b</a>' for u in people)
    return "📣 " + ", ".join(f'<a href="tg://user?id={u.id}">{escape(u.name)}</a>' for u in people)


def card_keyboard(occ: Occurrence, header: str = "", finished: bool = False, manage: bool = True) -> InlineKeyboardMarkup | None:
    """Напоминание: «Опоздаю» и «Перенести» (перенос продолжается в личке с ботом).
    Сообщение о начале и прошедшие ивенты — без кнопок. «Изменить» — только в личке (manage)."""
    if finished or is_start(header):
        return None
    ev = occ.event
    rows = [[
        Btn(text=LATE_LABEL, callback_data=RsvpCb(e=ev.id, k=occ.key, s=Rsvp.LATE.value).pack()),
        Btn(text="🕑 Перенести", callback_data=EvCb(a="move", e=ev.id, k=occ.key).pack()),
    ]]
    if manage:
        rows.append([Btn(text="✏️ Изменить", callback_data=EvCb(a="edit", e=ev.id, k=occ.key).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def week_text(
    title: str,
    occs: list[Occurrence],
    tz: str,
    people_of: dict[int, list[User]],
    team_size: int,
    my_answers: dict[tuple[int, int], Answer] | None = None,
) -> str:
    lines = [f"<b>{title}</b>", f"<i>время: {tz_label(tz)}</i>"]
    if not occs:
        lines.append("\nИвентов нет 🎉")
        return "\n".join(lines)
    day = None
    for occ in occs:
        d = fmt_day(occ.start, tz)
        if d != day:
            day = d
            lines.append(f"\n<b>{d}</b>")
        ev = occ.event
        mark = "🔁" if ev.is_recurring else "•"
        s = f"{mark} {fmt_time(occ.start, tz)}–{fmt_time(occ.end, tz)} {escape(ev.title)}"
        if occ.moved:
            s += " <i>(перенесён)</i>"
        people = people_of.get(ev.id, [])
        if not ev.is_recurring and len(people) < team_size:
            s += f" · <i>{_names(people)}</i>"
        if my_answers is not None and (a := my_answers.get((ev.id, occ.key))) and a.status == Rsvp.LATE:
            s += "  ⏰"
        lines.append(s)
    return "\n".join(lines)


def week_keyboard(offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        Btn(text="← Пред.", callback_data=WeekCb(o=offset - 1).pack()),
        Btn(text="Сегодня", callback_data=WeekCb(o=0).pack()),
        Btn(text="След. →", callback_data=WeekCb(o=offset + 1).pack()),
    ]])


def event_summary(ev: Event, viewer_tz: str, next_occ: Occurrence | None, people: list[User]) -> str:
    """Краткое описание ивента для превью и списка /events."""
    lines = [f"📅 <b>{escape(ev.title)}</b>"]
    if next_occ:
        tzs = sorted({u.tz for u in people} | {ev.tz})
        lines.append(f"🕙 {time_line(next_occ.start, next_occ.end, viewer_tz, tzs, now_ts())}")
    lines.append(f"🔁 {describe(ev)} · {fmt_duration(ev.duration_min)}")
    lines.append(f"👥 {'вся команда' if ev.is_recurring else _names(people)}")
    reminders = ", ".join(fmt_offset(m) for m in sorted(ev.reminders, reverse=True))
    lines.append(f"🔔 {reminders + ' и ' if reminders else ''}в момент начала")
    if ev.call_url:
        lines.append(f"🔗 {escape(ev.call_url)}")
    if ev.agenda:
        lines.append(f"📝 {escape(ev.agenda)}")
    return "\n".join(lines)
