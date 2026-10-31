"""HTTP-сервер: Mini App календаря и его API.

Запросы Mini App подписаны Telegram (initData) — по подписи узнаём, кто смотрит,
без отдельной авторизации.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

from . import logic
from .models import Occurrence, Rsvp, User
from .recurrence import describe
from .service import Service
from .timeutil import fmt_duration, tz_label

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "webapp"
MAX_RANGE = 120 * 86400


class WebServer:
    def __init__(self, svc: Service, token: str, public_url: str | None):
        self.svc = svc
        self.token = token
        self.public_url = public_url.rstrip("/") if public_url else None
        self.app = web.Application(middlewares=[self._no_cache])
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/app", self.index),
            web.get("/api/events", self.api_events),
            web.post("/api/rsvp", self.api_rsvp),
            web.post("/api/open", self.api_open),
            web.get("/api/me", self.api_me),
        ])

    # ---------- ссылки ----------

    def app_url(self) -> str | None:
        return f"{self.public_url}/app" if self.public_url else None

    # ---------- инфраструктура ----------

    @web.middleware
    async def _no_cache(self, request: web.Request, handler):
        resp = await handler(request)
        if request.path.startswith("/api/") or request.path in ("/", "/app"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    async def _user(self, request: web.Request) -> User:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        try:
            data = safe_parse_webapp_init_data(self.token, init_data)
        except ValueError:
            raise web.HTTPUnauthorized(text="bad initData")
        if not data.user:
            raise web.HTTPUnauthorized(text="no user")
        user = await self.svc.db.get_user(data.user.id)
        if not user:
            raise web.HTTPForbidden(text="not registered")
        return user

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    # ---------- API ----------

    async def _serialize(self, occ: Occurrence, user: User, team: list[User]) -> dict:
        ev = occ.event
        ids = set(logic.participant_ids(ev, team))
        people = [u for u in team if u.id in ids]
        answers = await self.svc.db.rsvps(ev.id, occ.key)
        mine = user.id in ids
        organizer = next((u for u in team if u.id == ev.organizer_id), None)
        my = answers.get(user.id)
        return {
            "e": ev.id, "k": occ.key, "title": ev.title,
            "start": occ.start, "end": occ.end, "moved": occ.moved,
            "recurring": ev.is_recurring, "online": ev.online_only,
            "repeat": describe(ev), "duration": fmt_duration(ev.duration_min),
            "agenda": ev.agenda,
            "url": ev.call_url if ev.call_url and logic.show_link(ev, people, answers) else None,
            "organizer": organizer.name if organizer else None,
            "mine": mine,
            "late": bool(my and my.status == Rsvp.LATE),
            "total": len(people),
            "lateNames": [u.name for u in people if (a := answers.get(u.id)) and a.status == Rsvp.LATE],
        }

    async def api_me(self, request: web.Request) -> web.Response:
        user = await self._user(request)
        return web.json_response({
            "id": user.id, "name": user.name, "tz": user.tz, "tzLabel": tz_label(user.tz),
        })

    async def api_events(self, request: web.Request) -> web.Response:
        user = await self._user(request)
        try:
            frm = int(request.query["from"])
            to = int(request.query["to"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="from/to required")
        if to <= frm or to - frm > MAX_RANGE:
            raise web.HTTPBadRequest(text="bad range")
        scope = request.query.get("scope", "mine")
        team = await self.svc.db.team()
        events = await self.svc.db.events()
        if scope != "team":
            events = [e for e in events if user.id in logic.participant_ids(e, team) or e.organizer_id == user.id]
        occs = await self.svc.db.occurrences(frm, to, events)
        return web.json_response({"events": [await self._serialize(o, user, team) for o in occs]})

    async def _occ_from_body(self, request: web.Request) -> tuple[User, Occurrence, dict]:
        user = await self._user(request)
        try:
            body = await request.json()
            eid, key = int(body["e"]), int(body["k"])
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="e/k required")
        occ = await self.svc.db.occurrence(eid, key)
        if not occ:
            raise web.HTTPNotFound(text="occurrence not found")
        return user, occ, body

    async def api_rsvp(self, request: web.Request) -> web.Response:
        user, occ, body = await self._occ_from_body(request)
        team = await self.svc.db.team()
        if user.id not in logic.participant_ids(occ.event, team):
            raise web.HTTPForbidden(text="not a participant")
        if body.get("s") != Rsvp.LATE.value:
            raise web.HTTPBadRequest(text="only 'late' is supported")
        current = (await self.svc.db.rsvps(occ.event.id, occ.key)).get(user.id)
        answer = logic.toggle_late(current)
        await self.svc.db.set_answer(occ.event.id, occ.key, user.id, answer)
        self.svc.refresh_cards_soon(occ.event.id, occ.key)  # карточки в Telegram — в фоне, ответ сразу
        return web.json_response(await self._serialize(occ, user, team))

    async def api_open(self, request: web.Request) -> web.Response:
        """Прислать карточку в личку — там перенос и редактирование."""
        user, occ, _ = await self._occ_from_body(request)
        await self.svc.send_card(user.id, occ)
        return web.json_response({"ok": True})

    # ---------- запуск ----------

    async def start(self, host: str, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("web server on %s:%s, public url: %s", host, port, self.public_url or "—")
        return runner
