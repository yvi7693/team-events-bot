"""Бизнес-правила: участники, напоминания, конфликты."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Answer, Event, Kind, Occurrence, Rsvp, User

CONFLICT_HORIZON_DAYS = 28  # на сколько вперёд проверять пересечения у регулярных


def default_reminders(kind: Kind, weekdays: list[int]) -> list[int]:
    """За сколько минут напоминать по умолчанию (момент начала шлётся всегда).

    - разовый: за день (успеть перестроить планы), за час, за 10 минут;
    - частый регулярный (дейли, 3+ раз в неделю): за 10 минут — он и так в привычке;
    - редкий регулярный (еженедельный созвон): за час и за 10 минут.
    """
    if kind == Kind.SINGLE:
        return [1440, 60, 10]
    if len(weekdays) >= 3:
        return [10]
    return [60, 10]


def participant_ids(ev: Event, team: list[User]) -> list[int]:
    """Регулярные — вся команда, разовые — выбранные участники."""
    if ev.is_recurring:
        return [u.id for u in team if u.active]
    active = {u.id for u in team if u.active}
    return [uid for uid in ev.participant_ids if uid in active]


def whole_team(ev: Event, team: list[User]) -> bool:
    """Ивент для всей команды: регулярный или разовый, куда позвали всех.

    Считается по составу участников, а не по ответам: «не буду» не делает ивент «не для всех».
    """
    if ev.is_recurring:
        return True
    active = {u.id for u in team if u.active}
    return bool(active) and active <= set(ev.participant_ids)


def can_edit(ev: Event, user_id: int, team: list[User]) -> bool:
    return user_id == ev.organizer_id or user_id in participant_ids(ev, team)


def can_delete(ev: Event, user_id: int) -> bool:
    return user_id == ev.organizer_id


def toggle_late(current: Answer | None) -> Answer:
    """Единственная кнопка ответа — «Опоздаю»: повторное нажатие отменяет."""
    if current and current.status == Rsvp.LATE:
        return Answer(Rsvp.YES)
    return Answer(Rsvp.LATE)


@dataclass
class Conflict:
    user_id: int
    mine: Occurrence
    other: Occurrence


def find_conflicts(
    candidate: list[Occurrence],
    candidate_participants: set[int],
    others: list[tuple[Occurrence, set[int]]],
) -> list[Conflict]:
    """Пересечения вхождений нового/изменённого ивента с чужими у общих участников."""
    result: list[Conflict] = []
    seen: set[tuple[int, int]] = set()
    for other, other_people in others:
        common = candidate_participants & other_people
        if not common:
            continue
        for mine in candidate:
            if mine.event.id == other.event.id:
                continue
            if mine.start < other.end and other.start < mine.end:
                for uid in sorted(common):
                    if (uid, other.event.id) in seen:
                        continue
                    seen.add((uid, other.event.id))
                    result.append(Conflict(uid, mine, other))
    return result
