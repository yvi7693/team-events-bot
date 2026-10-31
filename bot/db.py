"""SQLite-хранилище."""

from __future__ import annotations

import json
import time as _time

import aiosqlite

from .models import Answer, Event, Kind, Occurrence, Override, Rsvp, User
from .recurrence import expand

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT,
    tz TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    tz TEXT NOT NULL,
    start_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    organizer_id INTEGER NOT NULL,
    weekdays TEXT NOT NULL DEFAULT '[]',
    interval_weeks INTEGER NOT NULL DEFAULT 1,
    until_date TEXT,
    call_url TEXT,
    agenda TEXT,
    reminders TEXT NOT NULL DEFAULT '[]',
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (event_id, user_id)
);
CREATE TABLE IF NOT EXISTS overrides (
    event_id INTEGER NOT NULL,
    original_ts INTEGER NOT NULL,
    new_ts INTEGER,
    cancelled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, original_ts)
);
CREATE TABLE IF NOT EXISTS rsvps (
    event_id INTEGER NOT NULL,
    occ_key INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (event_id, occ_key, user_id)
);
-- журнал отправленных уведомлений: start входит в ключ, чтобы после переноса напоминания ушли заново
CREATE TABLE IF NOT EXISTS sent (
    event_id INTEGER NOT NULL,
    occ_key INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    offset_min INTEGER NOT NULL,
    target INTEGER NOT NULL,
    PRIMARY KEY (event_id, occ_key, start_ts, offset_min, target)
);
-- карточки вхождений, которые надо перерисовывать при изменении RSVP/времени
CREATE TABLE IF NOT EXISTS cards (
    event_id INTEGER NOT NULL,
    occ_key INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    header TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS cards_occ ON cards (event_id, occ_key);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def c(self) -> aiosqlite.Connection:
        assert self.conn is not None
        return self.conn

    # ---------- settings ----------

    async def get_setting(self, key: str) -> str | None:
        async with self.c.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.c.commit()

    async def team_chat_id(self) -> int | None:
        v = await self.get_setting("team_chat_id")
        return int(v) if v else None

    # ---------- users ----------

    @staticmethod
    def _user(row) -> User:
        return User(row["id"], row["name"], row["username"], row["tz"], active=bool(row["active"]))

    async def get_user(self, uid: int) -> User | None:
        async with self.c.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
        return self._user(row) if row else None

    async def upsert_user(self, uid: int, name: str, username: str | None, tz: str) -> User:
        await self.c.execute(
            """INSERT INTO users(id, name, username, tz) VALUES(?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, active=1""",
            (uid, name, username, tz),
        )
        await self.c.commit()
        return await self.get_user(uid)

    async def update_user(self, uid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.c.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))
        await self.c.commit()

    async def team(self) -> list[User]:
        async with self.c.execute("SELECT * FROM users WHERE active=1 ORDER BY name") as cur:
            return [self._user(r) for r in await cur.fetchall()]

    # ---------- events ----------

    async def _event(self, row) -> Event:
        async with self.c.execute("SELECT user_id FROM participants WHERE event_id=?", (row["id"],)) as cur:
            pids = [r["user_id"] for r in await cur.fetchall()]
        return Event(
            id=row["id"], title=row["title"], kind=Kind(row["kind"]), tz=row["tz"],
            start_date=row["start_date"], start_time=row["start_time"],
            duration_min=row["duration_min"], organizer_id=row["organizer_id"],
            weekdays=json.loads(row["weekdays"]), interval_weeks=row["interval_weeks"],
            until_date=row["until_date"], call_url=row["call_url"], agenda=row["agenda"],
            reminders=json.loads(row["reminders"]), participant_ids=pids,
        )

    async def get_event(self, eid: int) -> Event | None:
        async with self.c.execute("SELECT * FROM events WHERE id=? AND deleted=0", (eid,)) as cur:
            row = await cur.fetchone()
        return await self._event(row) if row else None

    async def events(self) -> list[Event]:
        async with self.c.execute("SELECT * FROM events WHERE deleted=0 ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [await self._event(r) for r in rows]

    async def create_event(self, ev: Event) -> Event:
        cur = await self.c.execute(
            """INSERT INTO events(title, kind, tz, start_date, start_time, duration_min, organizer_id,
                                  weekdays, interval_weeks, until_date, call_url, agenda, reminders, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev.title, ev.kind.value, ev.tz, ev.start_date, ev.start_time, ev.duration_min, ev.organizer_id,
             json.dumps(ev.weekdays), ev.interval_weeks, ev.until_date, ev.call_url, ev.agenda,
             json.dumps(ev.reminders), int(_time.time())),
        )
        ev.id = cur.lastrowid
        await self._save_participants(ev)
        await self.c.commit()
        return ev

    async def save_event(self, ev: Event) -> None:
        await self.c.execute(
            """UPDATE events SET title=?, tz=?, start_date=?, start_time=?, duration_min=?, weekdays=?,
                   interval_weeks=?, until_date=?, call_url=?, agenda=?, reminders=? WHERE id=?""",
            (ev.title, ev.tz, ev.start_date, ev.start_time, ev.duration_min, json.dumps(ev.weekdays),
             ev.interval_weeks, ev.until_date, ev.call_url, ev.agenda, json.dumps(ev.reminders), ev.id),
        )
        await self._save_participants(ev)
        await self.c.commit()

    async def _save_participants(self, ev: Event) -> None:
        await self.c.execute("DELETE FROM participants WHERE event_id=?", (ev.id,))
        await self.c.executemany(
            "INSERT INTO participants(event_id, user_id) VALUES(?, ?)",
            [(ev.id, uid) for uid in dict.fromkeys(ev.participant_ids)],
        )

    async def delete_event(self, eid: int) -> None:
        await self.c.execute("UPDATE events SET deleted=1 WHERE id=?", (eid,))
        await self.c.commit()

    async def move_occurrence_data(self, eid: int, old_key: int, new_key: int) -> None:
        """Ключ вхождения сменился (перенос разового / серии) — переносим ответы и карточки."""
        if old_key == new_key:
            return
        await self.c.execute("DELETE FROM rsvps WHERE event_id=? AND occ_key=?", (eid, new_key))
        await self.c.execute("UPDATE rsvps SET occ_key=? WHERE event_id=? AND occ_key=?", (new_key, eid, old_key))
        await self.c.execute("UPDATE cards SET occ_key=? WHERE event_id=? AND occ_key=?", (new_key, eid, old_key))
        await self.c.commit()

    async def drop_future_overrides(self, eid: int, after_ts: int) -> None:
        await self.c.execute("DELETE FROM overrides WHERE event_id=? AND original_ts>=?", (eid, after_ts))
        await self.c.commit()

    # ---------- overrides & occurrences ----------

    async def overrides(self, eid: int) -> dict[int, Override]:
        async with self.c.execute("SELECT * FROM overrides WHERE event_id=?", (eid,)) as cur:
            rows = await cur.fetchall()
        return {
            r["original_ts"]: Override(eid, r["original_ts"], r["new_ts"], bool(r["cancelled"]))
            for r in rows
        }

    async def set_override(self, eid: int, original_ts: int, new_ts: int | None, cancelled: bool = False) -> None:
        await self.c.execute(
            """INSERT INTO overrides(event_id, original_ts, new_ts, cancelled) VALUES(?, ?, ?, ?)
               ON CONFLICT(event_id, original_ts) DO UPDATE SET new_ts=excluded.new_ts, cancelled=excluded.cancelled""",
            (eid, original_ts, new_ts, int(cancelled)),
        )
        await self.c.commit()

    async def occurrences(self, frm: int, to: int, events: list[Event] | None = None) -> list[Occurrence]:
        events = events if events is not None else await self.events()
        result = []
        for ev in events:
            result.extend(expand(ev, frm, to, await self.overrides(ev.id)))
        result.sort(key=lambda o: (o.start, o.event.title))
        return result

    async def occurrence(self, eid: int, key: int) -> Occurrence | None:
        ev = await self.get_event(eid)
        if not ev:
            return None
        ovs = await self.overrides(eid)
        ov = ovs.get(key)
        if ov and ov.cancelled:
            return None
        # ищем вхождение с этим ключом в окрестности ключа и, если есть, нового времени
        anchor = ov.new_ts if ov and ov.new_ts else key
        lo, hi = min(key, anchor) - 86400, max(key, anchor) + 86400
        for occ in expand(ev, lo, hi, ovs):
            if occ.key == key:
                return occ
        return None

    async def next_occurrence(self, ev: Event, after: int) -> Occurrence | None:
        occs = expand(ev, after, after + 400 * 86400, await self.overrides(ev.id))
        return occs[0] if occs else None

    # ---------- rsvp ----------

    async def set_answer(self, eid: int, key: int, uid: int, answer: Answer) -> None:
        await self.c.execute(
            """INSERT INTO rsvps(event_id, occ_key, user_id, status, updated_at) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(event_id, occ_key, user_id)
               DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
            (eid, key, uid, answer.status.value, int(_time.time())),
        )
        await self.c.commit()

    async def rsvps(self, eid: int, key: int) -> dict[int, Answer]:
        async with self.c.execute(
            "SELECT user_id, status FROM rsvps WHERE event_id=? AND occ_key=?", (eid, key)
        ) as cur:
            rows = await cur.fetchall()
        # старые ответы («в офисе», «онлайн» и т.п.) больше ничего не значат — пропускаем
        return {r["user_id"]: Answer(Rsvp(r["status"])) for r in rows if r["status"] in Rsvp._value2member_map_}

    # ---------- sent log ----------

    async def mark_sent(self, occ: Occurrence, offset: int, target: int) -> bool:
        """True, если записали впервые (значит, нужно отправлять)."""
        cur = await self.c.execute(
            "INSERT OR IGNORE INTO sent(event_id, occ_key, start_ts, offset_min, target) VALUES(?, ?, ?, ?, ?)",
            (occ.event.id, occ.key, occ.start, offset, target),
        )
        await self.c.commit()
        return cur.rowcount == 1

    # ---------- cards ----------

    async def add_card(self, eid: int, key: int, chat_id: int, message_id: int, header: str = "") -> None:
        await self.c.execute(
            "INSERT OR REPLACE INTO cards(event_id, occ_key, chat_id, message_id, header) VALUES(?, ?, ?, ?, ?)",
            (eid, key, chat_id, message_id, header),
        )
        await self.c.commit()

    async def cards(self, eid: int, key: int | None = None) -> list[tuple[int, int, int, str]]:
        """[(occ_key, chat_id, message_id, header)]"""
        q = "SELECT occ_key, chat_id, message_id, header FROM cards WHERE event_id=?"
        args: tuple = (eid,)
        if key is not None:
            q, args = q + " AND occ_key=?", (eid, key)
        async with self.c.execute(q, args) as cur:
            return [(r["occ_key"], r["chat_id"], r["message_id"], r["header"]) for r in await cur.fetchall()]

    async def remove_card(self, chat_id: int, message_id: int) -> None:
        await self.c.execute("DELETE FROM cards WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        await self.c.commit()

    async def prune_cards(self, before_ts: int) -> None:
        await self.c.execute("DELETE FROM cards WHERE occ_key<?", (before_ts,))
        await self.c.commit()
