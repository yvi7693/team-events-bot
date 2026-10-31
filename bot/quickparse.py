"""Быстрое создание ивента одной строкой.

    /new Название | когда | [длительность] | [повтор] | [@участники] | [ссылка] | [повестка]

Первые два поля обязательны и идут по порядку, остальные распознаются по виду
и могут стоять в любом порядке:

    /new Дейли | 10:00 | 15м | по будням | https://meet.google.com/abc
    /new Ретро | пт 16:00 | 1ч | каждые 2 недели | Что улучшить в процессе
    /new Демо заказчику | 05.11 14:00 | 45м | @anna @oleg | https://zoom.us/j/1 | Показ MVP
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .timeutil import parse_duration, parse_time, parse_weekday, parse_when


@dataclass
class Draft:
    title: str = ""
    start: datetime | None = None
    duration_min: int = 30
    recurring: bool = False
    weekdays: list[int] = field(default_factory=list)
    interval_weeks: int = 1
    usernames: list[str] = field(default_factory=list)
    everyone: bool = False
    call_url: str | None = None
    agenda: str | None = None


class ParseError(ValueError):
    pass


_URL = re.compile(r"^(https?://\S+|meet\.google\.com/\S+|zoom\.us/\S+|t\.me/\S+)$", re.I)


def parse_recurrence(text: str) -> tuple[list[int], int] | None:
    t = text.strip().lower()
    if t in ("дейли", "daily", "по будням", "будни", "пн-пт"):
        return [0, 1, 2, 3, 4], 1
    if t in ("каждый день", "ежедневно"):
        return list(range(7)), 1
    interval = 1
    m = re.match(r"(?:каждые|раз в)\s+(\d+)\s+нед\w*\s*:?\s*(.*)", t)
    if m:
        interval = max(int(m.group(1)), 1)
        rest = m.group(2)
    else:
        m = re.match(r"(?:еженедельно|каждую неделю|каждый|каждую|каждое|weekly)\s*:?\s*(.*)", t)
        if not m:
            return None
        rest = m.group(1)
    days = []
    for token in re.split(r"[\s,/]+", rest):
        if not token:
            continue
        if "-" in token:
            a, b = (parse_weekday(x) for x in token.split("-", 1))
            if a is None or b is None:
                return None
            days.extend(range(a, b + 1))
            continue
        wd = parse_weekday(token)
        if wd is None:
            return None
        days.append(wd)
    return sorted(set(days)), interval


def parse_line(text: str, tz: str, now: datetime | None = None) -> Draft:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2 or not parts[0]:
        raise ParseError("Нужно минимум «Название | когда». Пример: /new Дейли | 10:00 | 15м | по будням")
    d = Draft(title=parts[0])

    for p in parts[2:]:
        if not p:
            continue
        if _URL.match(p):
            d.call_url = p if p.startswith("http") else "https://" + p
        elif (dur := parse_duration(p)) is not None and not re.fullmatch(r"\d+", p):
            d.duration_min = dur
        elif (rec := parse_recurrence(p)) is not None:
            d.recurring = True
            d.weekdays, d.interval_weeks = rec
        elif p.lower() in ("все", "вся команда"):
            d.everyone = True
        elif p.startswith("@"):
            d.usernames = [u.lstrip("@").lower() for u in p.split() if u.startswith("@")]
        else:
            d.agenda = p if d.agenda is None else d.agenda + "\n" + p

    when = parts[1]
    if d.recurring and parse_time(when) and not d.weekdays:
        raise ParseError("Укажи дни повтора, например «каждый пн,ср».")
    if d.recurring and parse_time(when):
        # время без даты: первое вхождение — ближайший подходящий день
        zone = ZoneInfo(tz)
        now = (now or datetime.now(zone)).astimezone(zone)
        t = parse_time(when)
        for i in range(8):
            cand = datetime.combine(now.date() + timedelta(days=i), t, tzinfo=zone)
            if cand.weekday() in d.weekdays and cand > now:
                d.start = cand
                break
    else:
        d.start = parse_when(when, tz, now)
    if d.start is None:
        raise ParseError(f"Не понял время «{when}». Примеры: 15:30, завтра 10:00, пт 11:00, 05.11 14:00")
    if d.recurring and not d.weekdays:
        d.weekdays = [d.start.weekday()]
    if d.recurring and d.start.weekday() not in d.weekdays:
        raise ParseError("Дата первого раза не попадает в дни повтора.")
    return d
