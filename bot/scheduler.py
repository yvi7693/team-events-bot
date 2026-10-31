"""Фоновый цикл рассылки напоминаний (в общий чат команды).

Раз в TICK секунд смотрит, у каких вхождений наступил момент напоминания, и шлёт его,
записывая факт отправки в журнал `sent`. Так рестарт бота не даёт ни дублей, ни потерь
(в пределах GRACE), а перенос ивента автоматически перепланирует напоминания.
"""

from __future__ import annotations

import asyncio
import logging

from .models import Occurrence
from .render import reminder_header
from .service import Service
from .timeutil import now_ts

log = logging.getLogger(__name__)

TICK = 20
GRACE = 15 * 60          # опоздавшее больше чем на 15 минут напоминание уже не шлём
MAX_LOOKAHEAD = 7 * 86400


def due_offsets(occ: Occurrence, now: int) -> list[int]:
    """Какие напоминания (в минутах до начала, 0 — старт) должны уйти прямо сейчас."""
    result = []
    for off in sorted(set(occ.event.reminders) | {0}, reverse=True):
        fire = occ.start - off * 60
        if fire <= now < fire + GRACE and now < occ.end:
            result.append(off)
    # если пропустили несколько (простой бота) — достаточно самого свежего
    return result[-1:] if result else []


async def tick(svc: Service) -> None:
    now = now_ts()
    lookahead = min(max((max(e.reminders, default=0) for e in await svc.db.events()), default=0) * 60,
                    MAX_LOOKAHEAD)
    occs = await svc.db.occurrences(now - GRACE - 6 * 3600, now + lookahead + 60)
    group = await svc.db.team_chat_id()
    if not group:  # напоминания живут только в общем чате; пока его нет — ждём
        return
    for occ in occs:
        for off in due_offsets(occ, now):
            await deliver(svc, occ, off, group, now)
    await svc.db.prune_cards(now - 3 * 86400)


async def deliver(svc: Service, occ: Occurrence, off: int, group: int, now: int) -> None:
    """Все напоминания — в общий чат с отметками участников; в личку бот сам не пишет."""
    left = -(-(occ.start - now) // 60)  # минут до начала, с округлением вверх
    header = reminder_header(0 if off == 0 else (off if left >= off - 2 else max(left, 1)))
    if await svc.db.mark_sent(occ, off, group):
        await svc.send_card(group, occ, header)


async def run(svc: Service) -> None:
    while True:
        try:
            await tick(svc)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK)
