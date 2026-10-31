"""Регистрация, профиль (часовой пояс, режим работы), привязка общего чата."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message, WebAppInfo,
)

from ..models import Mode
from ..service import Service
from ..web import WebServer
from ..timeutil import COMMON_TZ, fmt_time, now_ts, resolve_tz, tz_label

router = Router(name="common")
# в общем чате бот только напоминает — команды и настройки работают лишь в личке
router.message.filter(F.chat.type == "private")
# служебные события общего чата (без команд)
group_router = Router(name="group")

HELP = """<b>Команды</b>
/new — создать ивент
/week — ивенты на 7 дней
/calendar — календарь
/events — все ивенты
/tz — часовой пояс
/mode — офис или удалёнка по умолчанию
/team — команда
/cancel — прервать действие

<b>Быстро одной строкой</b>
<code>/new Дейли | 10:00 | 15м | по будням | ссылка</code>
<code>/new Демо | пт 14:00 | 1ч | @anna @oleg | онлайн</code>
"""


class Reg(StatesGroup):
    tz_manual = State()


class TzCb(CallbackData, prefix="tz"):
    tz: str


class ModeCb(CallbackData, prefix="mode"):
    m: str


def tz_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for label, tz in COMMON_TZ:
        row.append(Btn(text=label, callback_data=TzCb(tz=tz).pack()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn(text="✍️ Другой", callback_data=TzCb(tz="manual").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        Btn(text="🏢 Обычно в офисе", callback_data=ModeCb(m=Mode.OFFICE.value).pack()),
        Btn(text="💻 Обычно удалённо", callback_data=ModeCb(m=Mode.REMOTE.value).pack()),
    ]])


async def deep_link_kb(bot: Bot, payload: str, text: str) -> InlineKeyboardMarkup:
    me = await bot.me()
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=text, url=f"https://t.me/{me.username}?start={payload}")]])


@group_router.message(F.migrate_to_chat_id)
async def chat_migrated(message: Message, svc: Service) -> None:
    """Группа стала супергруппой (например, после назначения админов) — у неё новый id."""
    if await svc.db.team_chat_id() == message.chat.id:
        await svc.db.set_setting("team_chat_id", str(message.migrate_to_chat_id))


@group_router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def membership_changed(update: ChatMemberUpdated, svc: Service) -> None:
    """Чат становится командным, когда бота добавляет участник команды; удалили — привязка снимается."""
    joined = update.new_chat_member.status in ("member", "administrator")
    was_in = update.old_chat_member.status in ("member", "administrator", "creator", "restricted")
    chat_id = update.chat.id
    if not joined:
        if await svc.db.team_chat_id() == chat_id:
            await svc.db.set_setting("team_chat_id", "")
        return
    if was_in:  # например, повысили до админа — ничего не меняем
        return
    if not await svc.db.get_user(update.from_user.id):
        await svc.safe_send(
            chat_id, "Подключить бота к чату может только участник команды. Нажми «Старт» у бота в личке и добавь его снова.",
            reply_markup=await deep_link_kb(update.bot, "join", "Открыть бота"),
        )
        return
    await svc.db.set_setting("team_chat_id", str(chat_id))
    await svc.safe_send(
        chat_id,
        "👋 Буду напоминать здесь о встречах всей команды.\n\n"
        "Чтобы бот отмечал вас в напоминаниях и чтобы создавать ивенты, нажмите «Старт» у бота в личке.",
        reply_markup=await deep_link_kb(update.bot, "join", "Подключиться к боту"),
    )


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext, svc: Service) -> None:
    await state.clear()
    u = message.from_user
    existing = await svc.db.get_user(u.id)
    user = await svc.db.upsert_user(u.id, u.full_name, u.username, existing.tz if existing else await svc.team_tz())
    if command.args == "calendar" and existing:
        await message.answer("Нажми /calendar или кнопку «📅 Календарь» слева от поля ввода.")
        return
    if command.args == "new" and existing:
        from .create import start_wizard
        await start_wizard(message.chat.id, user, state, svc)
        return
    if existing:
        await message.answer(f"С возвращением, {escape(user.name)}! /help — все команды.")
        return
    await message.answer(
        f"Привет, {escape(user.name)}! Я напоминаю команде о встречах.\n\nВ каком ты часовом поясе?",
        reply_markup=tz_keyboard(),
    )


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("tz"))
async def tz_cmd(message: Message, command: CommandObject, svc: Service) -> None:
    if command.args:
        if tz := resolve_tz(command.args):
            await svc.db.update_user(message.from_user.id, tz=tz)
            await message.answer(f"Часовой пояс: <b>{tz_label(tz)}</b> (сейчас {fmt_time(now_ts(), tz)})")
        else:
            await message.answer("Не знаю такой пояс. Примеры: Москва, Europe/Berlin, UTC+5")
        return
    user = await svc.db.get_user(message.from_user.id)
    current = f"Сейчас: <b>{tz_label(user.tz)}</b>\n" if user else ""
    await message.answer(current + "Выбери часовой пояс:", reply_markup=tz_keyboard())


@router.callback_query(TzCb.filter())
async def tz_chosen(cb: CallbackQuery, callback_data: TzCb, state: FSMContext, svc: Service) -> None:
    if callback_data.tz == "manual":
        await state.set_state(Reg.tz_manual)
        await cb.message.answer("Напиши пояс: например <code>Europe/Berlin</code>, <code>Asia/Dubai</code> или <code>UTC+4</code>")
        await cb.answer()
        return
    await _set_tz(cb.message, cb.from_user.id, callback_data.tz, svc)
    await cb.answer()


@router.message(Reg.tz_manual, F.text)
async def tz_manual(message: Message, state: FSMContext, svc: Service) -> None:
    tz = resolve_tz(message.text)
    if not tz:
        await message.answer("Не знаю такой пояс. Примеры: Europe/Berlin, Asia/Dubai, UTC+4")
        return
    await state.clear()
    await _set_tz(message, message.from_user.id, tz, svc)


async def _set_tz(message: Message, uid: int, tz: str, svc: Service) -> None:
    await svc.db.update_user(uid, tz=tz)
    await message.answer(
        f"Пояс: <b>{tz_label(tz)}</b> (у тебя {fmt_time(now_ts(), tz)}).\n\nГде ты обычно работаешь?",
        reply_markup=mode_keyboard(),
    )


@router.message(Command("mode"))
async def mode_cmd(message: Message, svc: Service) -> None:
    user = await svc.db.get_user(message.from_user.id)
    current = ""
    if user:
        current = "Сейчас: " + ("💻 удалённо" if user.mode == Mode.REMOTE else "🏢 в офисе") + "\n"
    await message.answer(current + "Где ты обычно работаешь?", reply_markup=mode_keyboard())


@router.callback_query(ModeCb.filter())
async def mode_chosen(cb: CallbackQuery, callback_data: ModeCb, svc: Service) -> None:
    await svc.db.update_user(cb.from_user.id, mode=callback_data.m)
    label = "💻 удалённо" if callback_data.m == Mode.REMOTE.value else "🏢 в офисе"
    await cb.message.edit_text(f"Готово! Обычно: <b>{label}</b>.\n\n/new — создать ивент · /week — неделя · /help — все команды")
    await cb.answer()


@router.message(Command("team"))
async def team_cmd(message: Message, svc: Service) -> None:
    team = await svc.db.team()
    if not team:
        await message.answer("Пока никто не зарегистрировался. Каждому нужно нажать /start у бота в личке.")
        return
    now = now_ts()
    lines = ["<b>Команда</b>"]
    for u in team:
        mode = "💻" if u.mode == Mode.REMOTE else "🏢"
        lines.append(f"{mode} {escape(u.name)} — {tz_label(u.tz)}, {fmt_time(now, u.tz)}")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(Command("calendar"))
async def calendar_cmd(message: Message, svc: Service, web: WebServer) -> None:
    user = await svc.db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала нажми /start — выберем часовой пояс.")
        return
    app_url = web.app_url()
    if not app_url:
        await message.answer("Календарь не настроен: администратору нужно указать PUBLIC_URL (см. README).")
        return
    await message.answer(
        "📅 Календарь открывается прямо в Telegram. Ещё он есть в кнопке меню слева от поля ввода.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[Btn(text="📅 Открыть календарь", web_app=WebAppInfo(url=app_url))]]),
    )
