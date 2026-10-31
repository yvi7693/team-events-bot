"""Сквозной сценарий через настоящий Dispatcher aiogram и фейковую сессию Telegram API."""

import itertools, time, datetime as dt
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, EditMessageText, EditMessageReplyMarkup, AnswerCallbackQuery, GetMe
from aiogram.types import Update, Message, Chat, User as TgUser, CallbackQuery, ChatMemberUpdated, ChatMemberLeft, ChatMemberMember
from bot.db import Database
from bot.service import Service
from bot.handlers import common, create, events
from bot import scheduler
from bot.models import Answer, Rsvp

mid = itertools.count(1000)
LOG = []
class FakeSession(BaseSession):
    async def make_request(self, bot, method, timeout=None):
        LOG.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=42, is_bot=True, first_name="B", username="team_bot")
        if isinstance(method, SendMessage):
            return Message(message_id=next(mid), date=dt.datetime.now(), chat=Chat(id=method.chat_id, type="private" if method.chat_id>0 else "supergroup"), text=method.text, reply_markup=method.reply_markup)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup, AnswerCallbackQuery)):
            return True
        return True
    async def close(self): pass
    async def stream_content(self, *a, **k): yield b""

USERS = {1: ("Аня","anna"), 2: ("Олег","oleg")}
uid_c = itertools.count(1)
def tg(u): return TgUser(id=u, is_bot=False, first_name=USERS[u][0], username=USERS[u][1])
BOT_USER = TgUser(id=42, is_bot=True, first_name="B", username="team_bot")


def added_to_group(u, chat=-100):
    """Пользователь u добавил бота в группу."""
    return Update(update_id=next(uid_c), my_chat_member=ChatMemberUpdated(
        chat=Chat(id=chat, type="supergroup"), from_user=tg(u) if u in USERS else TgUser(id=u, is_bot=False, first_name="X"),
        date=dt.datetime.now(), old_chat_member=ChatMemberLeft(user=BOT_USER), new_chat_member=ChatMemberMember(user=BOT_USER)))


def msg(u, text, chat=None):
    chat = chat or u
    return Update(update_id=next(uid_c), message=Message(message_id=next(mid), date=dt.datetime.now(), chat=Chat(id=chat, type="private" if chat>0 else "supergroup"), from_user=tg(u), text=text))
def cb(u, data, chat=None):
    chat = chat or u
    m = Message(message_id=next(mid), date=dt.datetime.now(), chat=Chat(id=chat, type="private" if chat>0 else "supergroup"), text="x")
    return Update(update_id=next(uid_c), callback_query=CallbackQuery(id=str(next(uid_c)), from_user=tg(u), chat_instance="c", message=m, data=data))

def find_btn(prefix):
    for m in reversed(LOG):
        kb = getattr(m, "reply_markup", None)
        if kb and hasattr(kb, "inline_keyboard"):
            for row in kb.inline_keyboard:
                for b in row:
                    if b.callback_data and b.callback_data.startswith(prefix): return b.callback_data
    raise AssertionError("no button "+prefix)
def find_kb(chat):
    for m in reversed(LOG):
        if isinstance(m, SendMessage) and m.chat_id == chat and m.reply_markup:
            return m.reply_markup


def last_text(): 
    for m in reversed(LOG):
        if isinstance(m, (SendMessage, EditMessageText)): return m.text
def texts_to(chat): return [m.text for m in LOG if isinstance(m, SendMessage) and m.chat_id==chat]

async def test_full_flow():
    db = Database(":memory:"); await db.connect()
    try:
        await _flow(db)
    finally:
        await db.close()  # иначе поток aiosqlite не даст процессу завершиться после упавшего assert


async def _flow(db):
    bot = Bot("1:x", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    svc = Service(bot, db, "Europe/Moscow")
    dp = Dispatcher(storage=MemoryStorage()); dp["svc"] = svc
    dp.include_routers(common.group_router, common.router, create.router, events.router)
    F = lambda u: dp.feed_update(bot, u)

    # чужой человек добавил бота в группу — чат не привязывается
    await F(added_to_group(777))
    assert await db.team_chat_id() is None
    for u, tz in ((1,"Europe/Moscow"),(2,"Europe/Berlin")):
        await F(msg(u, "/start")); await F(cb(u, f"tz:{tz}"))
    assert (await db.get_user(2)).tz == "Europe/Berlin"
    # участник команды добавил бота — чат стал командным, команды в нём не работают
    await F(added_to_group(1))
    assert await db.team_chat_id() == -100
    # новые люди в чате: приветствие с кнопкой — только тем, кто ещё не нажимал «Старт»
    def joined(members, chat=-100):
        return Update(update_id=next(uid_c), message=Message(
            message_id=next(mid), date=dt.datetime.now(), chat=Chat(id=chat, type="supergroup"), from_user=tg(1),
            new_chat_members=members))
    newbie = TgUser(id=3, is_bot=False, first_name="Ира")
    other_bot = TgUser(id=99, is_bot=True, first_name="SomeBot")
    await F(joined([newbie, tg(1), other_bot]))
    hello = [m for m in LOG if isinstance(m, SendMessage) and m.chat_id == -100][-1]
    assert "Привет" in hello.text and 'tg://user?id=3">Ира' in hello.text
    assert "id=1" not in hello.text and "SomeBot" not in hello.text           # подключённых и ботов не зовём
    assert hello.reply_markup.inline_keyboard[0][0].url == "https://t.me/team_bot?start=join"
    sent = len(texts_to(-100))
    await F(joined([tg(2)]))                                                 # уже нажимал «Старт» — тишина
    await F(joined([newbie], chat=-555))                                     # чужая группа — тишина
    assert len(texts_to(-100)) == sent and texts_to(-555) == []

    sent_before = len(texts_to(-100))
    for command in ("/start", "/help", "/new", "/week", "/events", "/team", "/bind", "/calendar"):
        await F(msg(1, command, chat=-100))
    assert len(texts_to(-100)) == sent_before

    # мастер: разовый
    await F(msg(1, "/new")); await F(msg(1, "Демо заказчику"))
    await F(cb(1, "wz:kind:single")); await F(msg(1, "завтра 15:00"))
    await F(cb(1, "wz:dur:60")); await F(cb(1, "wz:p:2")); await F(cb(1, "wz:p_ok:"))
    await F(msg(1, "https://zoom.us/j/1")); await F(msg(1, "Показ MVP"))
    await F(cb(1, "wz:rem_ok:"))
    await F(cb(1, "wz:save:"))
    evs = await db.events(); assert len(evs)==1 and evs[0].participant_ids==[1,2], evs
    # в группе: отметки (вся команда → @all), ответы есть, переноса и правки нет
    group_card = texts_to(-100)[-1]
    assert "📣 @all" in group_card and 'tg://user?id=1"' in group_card and 'tg://user?id=2"' in group_card
    group_btns = [b.callback_data for row in find_kb(-100).inline_keyboard for b in row if b.callback_data]
    assert [d.split(":")[0] + ":" + d.split(":")[1] for d in group_btns] == ["ev:move"]   # у нового ивента «Опоздаю» нет
    assert not any("новый ивент" in t for t in texts_to(1) + texts_to(2))   # в личку — только подтверждение
    assert any("Ивент создан" in t for t in texts_to(1))

    # RSVP из группы
    key = find_btn("ev:move:1:").split(":")[3]
    await F(cb(2, f"r:1:{key}:late", chat=-100))
    assert await db.rsvps(1, int(key)) == {2: Answer(Rsvp.LATE)}
    await F(cb(1, f"r:1:{key}:office", chat=-100))              # старая кнопка — ничего не меняет
    assert await db.rsvps(1, int(key)) == {2: Answer(Rsvp.LATE)}

    # быстрый ввод регулярного + конфликт
    await F(msg(1, "/new Дейли | 10:00 | 15м | по будням | https://meet.google.com/abc"))
    await F(cb(1, "wz:save:"))
    assert len(await db.events()) == 2
    # регулярный: кнопки только для исключений, «не ответили» не показываем
    daily_card = texts_to(-100)[-1]
    assert daily_card.startswith("🆕 <b>Дейли</b> · новый ивент") and "Будут" not in daily_card
    daily_btns = [b.callback_data for row in find_kb(-100).inline_keyboard for b in row if b.callback_data]
    assert len(daily_btns) == 1 and daily_btns[0].startswith("ev:move")

    # разовый, пересекающийся с дейли -> предупреждение
    await F(msg(1, "/new Синк | пн 10:05 | 30м | все"))
    assert "Пересечения" in last_text(), last_text()
    await F(cb(1, "wz:cancel:"))

    # перенос разового через +30 из группы
    await F(cb(2, f"ev:move:1:{key}", chat=-100))
    await F(cb(2, f"mv:delta:1:{key}:one:1800"))
    occ = (await db.occurrences(0, 2**40, [await db.get_event(1)]))[0]
    assert occ.start == int(key)+1800
    assert await db.rsvps(1, occ.key)  # ответы переехали

    # редактирование названия
    await F(cb(1, f"ev:edit:1:{occ.key}")); await F(cb(1, f"ed:title:1:{occ.key}")); await F(msg(1, "Демо v2"))
    assert (await db.get_event(1)).title == "Демо v2"

    # серия: перенос всей серии вручную
    ev2 = await db.get_event(2); o2 = await db.next_occurrence(ev2, int(time.time()))
    await F(cb(1, f"ev:move:2:{o2.key}")); await F(cb(1, f"mv:scope:2:{o2.key}:series:0"))
    await F(cb(1, f"mv:manual:2:{o2.key}:series:0")); await F(msg(1, "10:30"))
    assert (await db.get_event(2)).start_time == "10:30"

    # неделя
    await F(msg(2, "/week"))
    assert "Твои ивенты" in last_text() and "Демо v2" in last_text() and "Дейли" in last_text()

    # группа стала супергруппой — бот переезжает на новый id
    await F(Update(update_id=next(uid_c), message=Message(
        message_id=next(mid), date=dt.datetime.now(), chat=Chat(id=-100, type="group"), from_user=tg(1),
        migrate_to_chat_id=-1001)))
    assert await db.team_chat_id() == -1001

    # удаление не-организатором запрещено
    await F(cb(2, f"ev:delete:2:{(await db.next_occurrence(await db.get_event(2),int(time.time()))).key}"))
    assert await db.get_event(2)
    await scheduler.tick(svc)
    await svc.wait_background()
