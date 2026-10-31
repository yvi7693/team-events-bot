"""Доменные модели. Все моменты времени — int unix timestamp в UTC."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    RECURRING = "recurring"  # регулярный: дейли, еженедельный созвон и т.п.
    SINGLE = "single"        # разовый


class Mode(StrEnum):
    OFFICE = "office"
    REMOTE = "remote"


class Rsvp(StrEnum):
    YES = "yes"         # буду
    LATE = "late"       # опоздаю
    NO = "no"           # не буду


class Place(StrEnum):
    """Где человек будет в этот раз (только для ивентов «офис + онлайн»)."""
    OFFICE = "office"
    ONLINE = "online"


@dataclass(frozen=True)
class Answer:
    status: Rsvp
    place: Place | None = None  # None — как в профиле



@dataclass
class User:
    id: int
    name: str
    username: str | None
    tz: str
    mode: Mode = Mode.OFFICE
    active: bool = True

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else self.name


@dataclass
class Event:
    id: int
    title: str
    kind: Kind
    tz: str
    start_date: str          # YYYY-MM-DD — дата (первого) проведения в tz ивента
    start_time: str          # HH:MM — локальное время начала в tz ивента
    duration_min: int
    organizer_id: int
    weekdays: list[int] = field(default_factory=list)  # 0=пн … 6=вс, только для регулярных
    interval_weeks: int = 1
    until_date: str | None = None
    call_url: str | None = None
    agenda: str | None = None
    reminders: list[int] = field(default_factory=list)  # за сколько минут напомнить (без 0)
    participant_ids: list[int] = field(default_factory=list)  # только для разовых
    online_only: bool = False  # проводится только онлайн: ссылка всегда в карточке, без выбора офис/онлайн

    @property
    def is_recurring(self) -> bool:
        return self.kind == Kind.RECURRING


@dataclass
class Override:
    """Изменение одного вхождения регулярного ивента (или переноса разового)."""
    event_id: int
    original_ts: int
    new_ts: int | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class Occurrence:
    event: Event
    key: int        # исходное время начала — стабильный идентификатор вхождения
    start: int
    end: int

    @property
    def moved(self) -> bool:
        return self.key != self.start
