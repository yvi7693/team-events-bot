import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.models import Answer, Place, Rsvp
from bot.web import WebServer
from tests.test_db import MSK, FakeBot, make_single, start_in  # noqa: F401  (env-фикстура ниже)
from bot.db import Database
from bot.models import Mode
from bot.service import Service

TOKEN = "123456:TEST-token"


def init_data(user_id: int, token: str = TOKEN) -> str:
    """Подписывает initData так же, как Telegram."""
    fields = {"auth_date": str(int(time.time())), "query_id": "q1",
              "user": json.dumps({"id": user_id, "first_name": "U"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
async def client(tmp_path):
    db = Database(str(tmp_path / "w.sqlite3"))
    await db.connect()
    svc = Service(FakeBot(), db, MSK)
    await db.upsert_user(1, "Аня", "anna", MSK)
    await db.upsert_user(2, "Олег", "oleg", "Europe/Berlin")
    await db.upsert_user(3, "Ира", "ira", MSK)
    await db.update_user(2, mode=Mode.REMOTE.value)
    web = WebServer(svc, TOKEN, "https://cal.example.com/")
    c = TestClient(TestServer(web.app))
    await c.start_server()
    c.svc, c.web = svc, web
    yield c
    await c.close()
    await db.close()


def hdr(uid: int) -> dict:
    return {"X-Telegram-Init-Data": init_data(uid)}


async def test_api_requires_valid_init_data(client):
    assert (await client.get("/api/me")).status == 401
    bad = {"X-Telegram-Init-Data": init_data(1, token="999:other")}
    assert (await client.get("/api/me", headers=bad)).status == 401
    assert (await client.get("/api/me", headers=hdr(99))).status == 403   # не зарегистрирован
    me = await (await client.get("/api/me", headers=hdr(1))).json()
    assert me["name"] == "Аня" and "ics" not in me


async def test_events_scope_and_rsvp(client):
    ev = await make_single(client.svc, 120, participants=(1, 3))
    now = int(time.time())
    q = f"/api/events?from={now}&to={now + 86400}"
    mine = await (await client.get(q, headers=hdr(2))).json()
    assert mine["events"] == []                                   # Олег не участник
    team = await (await client.get(q + "&scope=team", headers=hdr(2))).json()
    assert len(team["events"]) == 1 and team["events"][0]["mine"] is False and "late" in team["events"][0]

    e = (await (await client.get(q, headers=hdr(3))).json())["events"][0]
    assert e["url"] is None                                       # все в офисе — ссылку не показываем
    r = await client.post("/api/rsvp", headers=hdr(3), json={"e": e["e"], "k": e["k"], "s": "late"})
    upd = await r.json()
    assert upd["late"] is True and upd["lateNames"] == ["Ира"]
    assert await client.svc.db.rsvps(ev.id, e["k"]) == {3: Answer(Rsvp.LATE)}
    again = await (await client.post("/api/rsvp", headers=hdr(3), json={"e": e["e"], "k": e["k"], "s": "late"})).json()
    assert again["late"] is False                                 # повторное нажатие отменяет
    await client.svc.wait_background()                            # карточки обновляются в фоне

    # чужой ивент и прежние кнопки «буду / офис» больше не принимаются
    assert (await client.post("/api/rsvp", headers=hdr(2), json={"e": e["e"], "k": e["k"], "s": "late"})).status == 403
    assert (await client.post("/api/rsvp", headers=hdr(3), json={"e": e["e"], "k": e["k"], "s": "online"})).status == 400


async def test_app_page_served(client):
    r = await client.get("/app")
    assert r.status == 200 and "telegram-web-app.js" in await r.text()


async def test_ics_removed(client):
    assert (await client.get("/ics/1-abc.ics")).status == 404
