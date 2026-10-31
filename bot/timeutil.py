"""Часовые пояса, разбор и форматирование дат."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, available_timezones

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]

# Популярные пояса для кнопок выбора
COMMON_TZ = [
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Минск", "Europe/Minsk"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Ереван", "Asia/Yerevan"),
    ("Берлин", "Europe/Berlin"),
    ("Лондон", "Europe/London"),
]
TZ_LABELS = {tz: label for label, tz in COMMON_TZ}

_WEEKDAY_ALIASES = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вто": 1, "вторник": 1,
    "ср": 2, "сре": 2, "среда": 2, "среду": 2,
    "чт": 3, "чет": 3, "четверг": 3,
    "пт": 4, "пят": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "вос": 6, "воскресенье": 6,
}


def resolve_tz(text: str) -> str | None:
    """Принимает 'Москва', 'Europe/Berlin', 'msk', 'UTC+3' и т.п."""
    t = text.strip()
    for label, tz in COMMON_TZ:
        if t.lower() == label.lower():
            return tz
    aliases = {"msk": "Europe/Moscow", "мск": "Europe/Moscow", "utc": "UTC", "gmt": "UTC"}
    if t.lower() in aliases:
        return aliases[t.lower()]
    m = re.fullmatch(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})", t, re.I)
    if m:
        hours = int(m.group(2))
        if hours > 14:
            return None
        if hours == 0:
            return "UTC"
        # Etc/GMT имеет инвертированный знак
        sign = "-" if m.group(1) == "+" else "+"
        return f"Etc/GMT{sign}{hours}"
    for name in available_timezones():
        if name.lower() == t.lower():
            return name
    return None


def tz_label(tz: str) -> str:
    if tz in TZ_LABELS:
        return TZ_LABELS[tz]
    if tz.startswith("Etc/GMT"):
        rest = tz[len("Etc/GMT"):]
        sign = "+" if rest.startswith("-") else "-"
        return f"UTC{sign}{rest[1:]}"
    return tz.split("/")[-1].replace("_", " ")


def to_ts(d: date, t: time, tz: str) -> int:
    return int(datetime.combine(d, t, tzinfo=ZoneInfo(tz)).timestamp())


def from_ts(ts: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def now_ts() -> int:
    return int(datetime.now().timestamp())


def fmt_time(ts: int, tz: str) -> str:
    return from_ts(ts, tz).strftime("%H:%M")


def fmt_day(ts: int, tz: str) -> str:
    dt = from_ts(ts, tz)
    return f"{WEEKDAYS_SHORT[dt.weekday()]}, {dt.day} {MONTHS_GEN[dt.month - 1]}"


def fmt_dt(ts: int, tz: str) -> str:
    return f"{fmt_day(ts, tz)}, {fmt_time(ts, tz)}"


def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def fmt_offset(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return "за 1 день" if d == 1 else f"за {d} дн."
    return f"за {fmt_duration(minutes)}"


def parse_duration(text: str) -> int | None:
    """'30', '30м', '1ч', '1ч30м', '1.5ч', '90 мин', '1h30m'."""
    t = text.strip().lower().replace(" ", "").replace(",", ".")
    if re.fullmatch(r"\d+", t):
        return int(t) or None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:ч|час|часа|часов|h)", t)
    if m:
        return int(float(m.group(1)) * 60) or None
    m = re.fullmatch(r"(?:(\d+)(?:ч|h))?(?:(\d+)(?:м|мин|минут|минуты|m|min))?", t)
    if m and (m.group(1) or m.group(2)):
        return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) or None
    return None


def parse_time(text: str) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def parse_weekday(token: str) -> int | None:
    return _WEEKDAY_ALIASES.get(token.strip().lower().rstrip("."))


def parse_when(text: str, tz: str, now: datetime | None = None) -> datetime | None:
    """Разбирает момент начала в поясе tz.

    Поддерживает: '15:30' (сегодня, а если прошло — завтра), 'завтра 10:00',
    'сегодня 18:00', 'послезавтра 9:30', 'пт 11:00', '02.11 15:30', '02.11.2026 15:30'.
    """
    zone = ZoneInfo(tz)
    now = (now or datetime.now(zone)).astimezone(zone)
    parts = text.strip().lower().split()
    if not parts:
        return None
    t = parse_time(parts[-1])
    if t is None:
        return None
    day_part = " ".join(parts[:-1]).replace("в ", "").strip()
    today = now.date()

    if not day_part:
        d = today
        if datetime.combine(d, t, tzinfo=zone) <= now:
            d += timedelta(days=1)
    elif day_part == "сегодня":
        d = today
    elif day_part == "завтра":
        d = today + timedelta(days=1)
    elif day_part == "послезавтра":
        d = today + timedelta(days=2)
    elif (wd := parse_weekday(day_part)) is not None:
        delta = (wd - today.weekday()) % 7
        d = today + timedelta(days=delta)
        if datetime.combine(d, t, tzinfo=zone) <= now:
            d += timedelta(days=7)
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", day_part)
        if not m:
            return None
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and d < today:
            d = date(year + 1, month, day)
    return datetime.combine(d, t, tzinfo=zone)


def week_bounds(ts: int, tz: str, offset_weeks: int = 0) -> tuple[int, int]:
    """Ближайшие 7 дней начиная с сегодняшнего (00:00 в поясе tz), со сдвигом на offset_weeks недель."""
    first = from_ts(ts, tz).date() + timedelta(weeks=offset_weeks)
    return to_ts(first, time(0, 0), tz), to_ts(first + timedelta(days=7), time(0, 0), tz)
