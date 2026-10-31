"""Интеграционные тесты хранилища, планировщика и переносов на фейковом боте."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot import scheduler
from bot.db import Database
from bot.models import Answer, Event, Kind, Rsvp
from bot.service import Service

MSK = "Europe/Moscow"


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None, **kw):
        self.edited.append((chat_id, message_id, text))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edited.append((chat_id, message_id, ""))

    def to(self, chat_id):
        return [t for c, t, _ in self.sent if c == chat_id]


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    await db.connect()
    bot = FakeBot()
    svc = Service(bot, db, MSK)
    await db.upsert_user(1, "Аня", "anna", MSK)
    await db.upsert_user(2, "Олег", "oleg", "Europe/Berlin")
    await db.upsert_user(3, "Ира", "ira", MSK)
    await db.set_setting("team_chat_id", "-100")
    yield svc, bot
    await db.close()


def start_in(minutes: int) -> datetime:
    return (datetime.now(ZoneInfo(MSK)) + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


async def make_single(svc, minutes, participants=(1, 3), reminders=(60, 10)):
    s = start_in(minutes)
    ev = Event(id=0, title="Демо", kind=Kind.SINGLE, tz=MSK, start_date=s.date().isoformat(),
               start_time=s.strftime("%H:%M"), duration_min=30, organizer_id=1,
               reminders=list(reminders), participant_ids=list(participants), call_url="https://meet/x")
    return await svc.db.create_event(ev)


async def test_reminders_go_only_to_group(env):
    svc, bot = env
    await make_single(svc, 9)                    # участники 1 и 3 из трёх
    await scheduler.tick(svc)
    await scheduler.tick(svc)                    # повторный тик не дублирует
    assert len(bot.to(-100)) == 1                # все напоминания — в общий чат, даже если ивент не для всех
    assert bot.to(1) == bot.to(2) == bot.to(3) == []   # в личку бот сам не пишет
    text = bot.to(-100)[0]
    assert 'tg://user?id=1">Аня' in text and 'tg://user?id=3">Ира' in text and "id=2" not in text


async def test_reminder_card_structure(env):
    svc, bot = env
    ev = await make_single(svc, 9)
    ev.agenda = "тест"
    await svc.db.save_event(ev)
    await scheduler.tick(svc)
    lines = bot.to(-100)[0].split("\n")
    # ⏰ Демо · через N мин / сегодня, 12:10–12:25 / пусто / Повестка / тест / пусто / ссылка / пусто / отметки
    assert lines[0].startswith("⏰ <b>Демо</b> · через ")
    assert lines[1].split(",")[0] in ("сегодня", "завтра") and "–" in lines[1]
    assert lines[2:5] == ["", "<b>Повестка</b>", "тест"]
    assert lines[6] == '🔗 <a href="https://meet/x">Подключиться к звонку</a>' and lines[8].startswith("📣 ")
    rows = [[b.text for b in row] for row in bot.sent[-1][2].inline_keyboard]
    assert rows == [["⏰ Опоздаю", "🕑 Перенести"]]


async def test_start_message_has_no_buttons_and_text_link(env):
    from bot.render import card_header, reminder_header
    svc, bot = env
    ev = await make_single(svc, 120)
    occ = (await svc.db.occurrences(0, 2**40))[0]
    text, kb = await svc.render(occ, -100, reminder_header(0))
    assert kb is None and 'href="https://meet/x"' in text   # в начале — без кнопок, ссылка текстом
    text, kb = await svc.render(occ, -100, card_header("⏰", "через 10 мин"))
    assert 'href="https://meet/x"' in text and kb is not None   # и в обычном напоминании — ссылкой в тексте


async def test_move_single_keeps_rsvp_and_resends_reminders(env):
    svc, bot = env
    ev = await make_single(svc, 9)
    occ = (await svc.db.occurrences(0, 2**40))[0]
    await svc.db.set_answer(ev.id, occ.key, 3, Answer(Rsvp.LATE))
    await scheduler.tick(svc)
    assert len(bot.to(-100)) == 1

    actor = await svc.db.get_user(1)
    await svc.move(ev, occ, occ.start + 3600, "one", actor)
    moved = (await svc.db.occurrences(0, 2**40))[0]
    assert moved.start == occ.start + 3600
    assert (await svc.db.rsvps(ev.id, moved.key)) == {3: Answer(Rsvp.LATE)}
    assert any("Перенос" in t for t in bot.to(-100))    # анонс — в общий чат
    assert not any("Перенос" in t for t in bot.to(1) + bot.to(3))


async def test_move_one_occurrence_of_series(env):
    svc, bot = env
    s = start_in(24 * 60)
    ev = await svc.db.create_event(Event(
        id=0, title="Дейли", kind=Kind.RECURRING, tz=MSK, start_date=s.date().isoformat(),
        start_time=s.strftime("%H:%M"), duration_min=15, organizer_id=1, weekdays=list(range(7)), reminders=[10]))
    occs = await svc.db.occurrences(s.timestamp() - 1, s.timestamp() + 3 * 86400)
    first, second = occs[0], occs[1]
    await svc.move(ev, first, first.start + 1800, "one", await svc.db.get_user(1))
    occs2 = await svc.db.occurrences(s.timestamp() - 1, s.timestamp() + 3 * 86400)
    assert occs2[0].key == first.key and occs2[0].start == first.start + 1800
    assert occs2[1].start == second.start                 # остальные не тронуты


async def test_conflict_detected(env):
    svc, _ = env
    a = await make_single(svc, 120, participants=(1, 2))
    s = start_in(130)
    b = Event(id=0, title="Другое", kind=Kind.SINGLE, tz=MSK, start_date=s.date().isoformat(),
              start_time=s.strftime("%H:%M"), duration_min=30, organizer_id=3, participant_ids=[2, 3])
    conflicts = await svc.conflicts(b, svc.candidate_occurrences(b), {2, 3})
    assert [(c.user_id, c.other.event.id) for c in conflicts] == [(2, a.id)]


async def test_card_refresh_on_rsvp(env):
    svc, bot = env
    ev = await make_single(svc, 120)
    occ = (await svc.db.occurrences(0, 2**40))[0]
    await svc.send_card(-100, occ)
    await svc.db.set_answer(ev.id, occ.key, 1, Answer(Rsvp.LATE))
    await svc.refresh_cards(ev.id, occ.key)
    assert bot.edited and bot.edited[-1][2].startswith("📅 <b>Демо</b>")


async def test_group_card_mentions_and_buttons(env):
    svc, _ = env
    s = start_in(120)
    await svc.db.create_event(Event(
        id=0, title="Дейли", kind=Kind.RECURRING, tz=MSK, start_date=s.date().isoformat(),
        start_time=s.strftime("%H:%M"), duration_min=15, organizer_id=1, weekdays=list(range(7)), reminders=[10]))
    occ = (await svc.db.occurrences(s.timestamp() - 1, s.timestamp() + 3600))[0]
    from bot.render import card_header, reminder_header
    text, kb = await svc.render(occ, -100, reminder_header(10))
    assert text.splitlines()[-1].startswith("📣 @all")
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [["⏰ Опоздаю", "🕑 Перенести"]]
    _, new_kb = await svc.render(occ, -100, card_header("🆕", "новый ивент"))   # «Опоздаю» — только в напоминании
    assert [[b.text for b in row] for row in new_kb.inline_keyboard] == [["🕑 Перенести"]]
    _, dm_kb = await svc.render(occ, 1)                       # в личке ещё и «Изменить»
    assert [[b.text for b in row] for row in dm_kb.inline_keyboard] == [["🕑 Перенести"], ["✏️ Изменить"]]


async def test_old_database_still_works(tmp_path):
    """База прежней версии: лишние колонки (mode, online_only, place) и старые ответы «в офисе / онлайн»."""
    import aiosqlite
    path = str(tmp_path / "old.sqlite3")
    async with aiosqlite.connect(path) as c:
        await c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT, tz TEXT NOT NULL, "
                        "mode TEXT NOT NULL DEFAULT 'office', active INTEGER NOT NULL DEFAULT 1)")
        await c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL, "
                        "tz TEXT NOT NULL, start_date TEXT NOT NULL, start_time TEXT NOT NULL, duration_min INTEGER NOT NULL, "
                        "organizer_id INTEGER NOT NULL, weekdays TEXT NOT NULL DEFAULT '[]', interval_weeks INTEGER NOT NULL DEFAULT 1, "
                        "until_date TEXT, call_url TEXT, agenda TEXT, reminders TEXT NOT NULL DEFAULT '[]', "
                        "online_only INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
        await c.execute("CREATE TABLE rsvps (event_id INTEGER NOT NULL, occ_key INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                        "status TEXT NOT NULL, place TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY (event_id, occ_key, user_id))")
        await c.executemany("INSERT INTO rsvps VALUES (1, 100, ?, ?, NULL, 0)", [(1, "office"), (2, "online"), (3, "late")])
        await c.commit()
    db = Database(path)
    await db.connect()
    try:
        u = await db.upsert_user(5, "Петя", None, MSK)
        assert u.name == "Петя"
        ev = await db.create_event(Event(id=0, title="X", kind=Kind.SINGLE, tz=MSK, start_date="2030-01-01",
                                         start_time="10:00", duration_min=30, organizer_id=5))
        assert (await db.get_event(ev.id)).title == "X"
        assert await db.rsvps(1, 100) == {3: Answer(Rsvp.LATE)}          # старые «офис / онлайн» пропускаются
        await db.set_answer(1, 100, 1, Answer(Rsvp.LATE))
        assert (await db.rsvps(1, 100))[1] == Answer(Rsvp.LATE)
    finally:
        await db.close()


async def test_late_shown_in_card(env):
    svc, _ = env
    ev = await make_single(svc, 120, participants=(1, 3))
    occ = (await svc.db.occurrences(0, 2**40))[0]
    text, _ = await svc.render(occ, -100)
    assert "Опоздают" not in text
    await svc.db.set_answer(ev.id, occ.key, 3, Answer(Rsvp.LATE))
    text, _ = await svc.render(occ, -100)
    assert "⏰ Опоздает: Ира" in text
    await svc.db.set_answer(ev.id, occ.key, 1, Answer(Rsvp.LATE))
    text, _ = await svc.render(occ, -100)
    assert "⏰ Опоздают: Аня, Ира" in text


async def test_background_refresh_coalesces(env):
    svc, bot = env
    ev = await make_single(svc, 120)
    occ = (await svc.db.occurrences(0, 2**40))[0]
    for chat in (-100, 1, 3):
        await svc.send_card(chat, occ)
    # пять быстрых ответов подряд — не пять перерисовок, а одна-две с последним состоянием
    for st in (Rsvp.LATE, Rsvp.YES, Rsvp.LATE, Rsvp.YES, Rsvp.LATE):
        await svc.db.set_answer(ev.id, occ.key, 1, Answer(st))
        svc.refresh_cards_soon(ev.id, occ.key)
    await svc.wait_background()
    assert 3 <= len(bot.edited) <= 6                      # 3 карточки × 1–2 прохода
    last = {chat: text for chat, _, text in bot.edited}
    assert all(t.startswith("📅 <b>Демо</b>") for t in last.values())


async def test_no_group_no_reminders(env):
    svc, bot = env
    await svc.db.set_setting("team_chat_id", "")
    await make_single(svc, 9)
    await scheduler.tick(svc)
    assert bot.sent == []                         # без общего чата бот молчит, в личку не дублирует
