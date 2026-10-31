"""Развёртка ивентов во вхождения на заданном интервале времени.

Регулярные ивенты разворачиваются в поясе организатора: дейли в 10:00 по Москве
остаётся в 10:00 по Москве, даже если у кого-то из команды сменилось летнее время.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Event, Occurrence, Override
from .timeutil import WEEKDAYS_SHORT, parse_time, to_ts


def base_start(ev: Event) -> int:
    """Начало первого вхождения по расписанию (без учёта переносов)."""
    return to_ts(date.fromisoformat(ev.start_date), parse_time(ev.start_time), ev.tz)


def _base_starts(ev: Event, frm: int, to: int) -> list[int]:
    t = parse_time(ev.start_time)
    first = date.fromisoformat(ev.start_date)
    if not ev.is_recurring:
        return [to_ts(first, t, ev.tz)]

    # с запасом в 1 день по краям — из-за разницы поясов
    from_day = max(first, date.fromtimestamp(frm) - timedelta(days=1))
    to_day = date.fromtimestamp(to) + timedelta(days=1)
    if ev.until_date:
        to_day = min(to_day, date.fromisoformat(ev.until_date))
    first_monday = first - timedelta(days=first.weekday())

    result = []
    d = from_day
    while d <= to_day:
        if d.weekday() in ev.weekdays:
            week_no = (d - first_monday).days // 7
            if week_no % max(ev.interval_weeks, 1) == 0:
                result.append(to_ts(d, t, ev.tz))
        d += timedelta(days=1)
    return result


def expand(ev: Event, frm: int, to: int, overrides: dict[int, Override] | None = None) -> list[Occurrence]:
    """Вхождения ивента, которые пересекаются с [frm, to)."""
    overrides = overrides or {}
    dur = ev.duration_min * 60
    # перенесённое вхождение могло уехать с исходного места — расширяем окно поиска
    slack = 14 * 86400 if overrides else 0
    occs = []
    for key in _base_starts(ev, frm - dur - slack, to + slack):
        ov = overrides.get(key)
        if ov and ov.cancelled:
            continue
        start = ov.new_ts if ov and ov.new_ts else key
        if start < to and start + dur > frm:
            occs.append(Occurrence(ev, key, start, start + dur))
    occs.sort(key=lambda o: o.start)
    return occs


def describe(ev: Event) -> str:
    if not ev.is_recurring:
        return "разовый"
    days = sorted(ev.weekdays)
    if days == [0, 1, 2, 3, 4] and ev.interval_weeks == 1:
        return "по будням"
    if days == list(range(7)) and ev.interval_weeks == 1:
        return "каждый день"
    names = ", ".join(WEEKDAYS_SHORT[d] for d in days)
    if ev.interval_weeks == 1:
        return f"еженедельно: {names}"
    return f"раз в {ev.interval_weeks} нед.: {names}"
