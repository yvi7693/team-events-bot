"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeDefault,
    MenuButtonDefault, MenuButtonWebApp, WebAppInfo,
)

from . import scheduler
from .db import Database
from .handlers import common, create, events
from .service import Service
from .web import WebServer

COMMANDS = [
    BotCommand(command="new", description="Создать ивент"),
    BotCommand(command="week", description="Ивенты на неделю"),
    BotCommand(command="calendar", description="Календарь"),
    BotCommand(command="events", description="Все ивенты"),
    BotCommand(command="tz", description="Мой часовой пояс"),
    BotCommand(command="team", description="Команда"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


async def update_menu_button(bot: Bot, web: WebServer) -> None:
    """Кнопка «Календарь» слева от поля ввода в личке с ботом."""
    if url := web.app_url():
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="📅 Календарь", web_app=WebAppInfo(url=url)))
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def watch_public_url(bot: Bot, web: WebServer, path: str) -> None:
    """Временные туннели меняют адрес на ходу — подхватываем новый из файла без рестарта."""
    log = logging.getLogger("public_url")
    while True:
        try:
            with open(path) as f:
                url = f.read().strip().rstrip("/") or None
            if url and url != web.public_url:
                web.public_url = url
                await update_menu_button(bot, web)
                log.info("public url changed: %s", url)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("cannot update public url")
        await asyncio.sleep(15)


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ["BOT_TOKEN"]
    db = Database(os.getenv("DB_PATH", "data/bot.sqlite3"))
    os.makedirs(os.path.dirname(db.path) or ".", exist_ok=True)
    await db.connect()

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    svc = Service(bot, db, os.getenv("TEAM_TZ", "Europe/Moscow"))
    if chat := os.getenv("TEAM_CHAT_ID"):
        await db.set_setting("team_chat_id", chat)

    web = WebServer(svc, token, os.getenv("PUBLIC_URL") or None)
    runner = await web.start(os.getenv("WEB_HOST", "0.0.0.0"), int(os.getenv("WEB_PORT", "8080")))

    dp = Dispatcher(storage=MemoryStorage())
    dp["svc"] = svc
    dp["web"] = web
    dp.include_routers(common.group_router, common.router, create.router, events.router)
    # меню команд — только в личке; в общем чате бот ничего не настраивает
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    # текст на пустом экране бота до /start и в его профиле
    await bot.set_my_description("Напоминаю команде о созвонах и встречах, собираю «буду / не буду» "
                                 "и присылаю ссылку на звонок. Нажми «Старт».")
    await bot.set_my_short_description("Напоминания о встречах команды")
    await update_menu_button(bot, web)

    tasks = [asyncio.create_task(scheduler.run(svc))]
    if url_file := os.getenv("PUBLIC_URL_FILE"):
        tasks.append(asyncio.create_task(watch_public_url(bot, web, url_file)))
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
