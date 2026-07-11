"""
Телеграм-бот для группового опроса "Приду / Пропущу / Не уверен"
с возможностью добавить игрока за себя и убрать добавленного.

Хранение данных — в памяти (сбрасывается при перезапуске бота).
Запуск опроса — по команде /poll и/или автоматически по расписанию.
"""

import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "").strip()  # формат "HH:MM", пусто = не запускать по расписанию
SCHEDULE_CHAT_ID = os.getenv("SCHEDULE_CHAT_ID", "").strip()  # чат, куда слать опрос по расписанию

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

STATUS_COME = "come"
STATUS_SKIP = "skip"
STATUS_UNSURE = "unsure"

STATUS_LABELS = {
    STATUS_COME: "Приду",
    STATUS_SKIP: "Пропущу",
    STATUS_UNSURE: "Пока не уверен",
}

FULL_SQUAD_SIZE = 12  # при таком числе идущих считаем комплект набранным

# ---------------------------------------------------------------------------
# Хранилище данных опроса (в памяти).
# Ключ — chat_id, значение — состояние опроса в этом чате.
# ---------------------------------------------------------------------------

polls: dict[int, dict] = {}
_player_id_counter = 0


def new_poll_state() -> dict:
    return {
        "title": None,
        "message_id": None,
        "announced_full": False,
        "closed": False,
        "responses": {},       # user_id -> {"username": str, "status": str}
        "added_players": {},   # player_id -> {"name": str, "added_by": int, "added_by_username": str}
    }


def get_poll(chat_id: int) -> dict:
    if chat_id not in polls:
        polls[chat_id] = new_poll_state()
    return polls[chat_id]


def display_name(user) -> str:
    # Ссылка вида tg://user?id=... работает даже если у человека не задан @username —
    # так все участники выглядят и ведут себя одинаково: кликабельное имя на профиль.
    visible_name = f"@{user.username}" if user.username else user.full_name
    safe_name = html.escape(visible_name)
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'


# ---------------------------------------------------------------------------
# FSM для добавления игрока
# ---------------------------------------------------------------------------

class AddPlayer(StatesGroup):
    waiting_name = State()


# ---------------------------------------------------------------------------
# Клавиатура и текст опроса
# ---------------------------------------------------------------------------

def count_come(poll: dict) -> int:
    responses_come = sum(1 for d in poll["responses"].values() if d["status"] == STATUS_COME)
    return responses_come + len(poll["added_players"])


def build_keyboard(poll: dict) -> InlineKeyboardMarkup:
    is_full = count_come(poll) >= FULL_SQUAD_SIZE
    rows = []
    if not is_full:
        rows.append([InlineKeyboardButton(text="✅ Приду", callback_data="status:come")])
    rows.append([InlineKeyboardButton(text="❌ Пропущу", callback_data="status:skip")])
    rows.append([InlineKeyboardButton(text="❔ Пока не уверен", callback_data="status:unsure")])
    if not is_full:
        rows.append([InlineKeyboardButton(text="➕ Добавить игрока", callback_data="add_player")])
    rows.append([InlineKeyboardButton(text="➖ Убрать добавленного игрока", callback_data="remove_player_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_poll_text(poll: dict) -> str:
    come, skip, unsure = [], [], []
    for data in poll["responses"].values():
        label = data["username"]
        if data["status"] == STATUS_COME:
            come.append(label)
        elif data["status"] == STATUS_SKIP:
            skip.append(label)
        elif data["status"] == STATUS_UNSURE:
            unsure.append(label)

    for player in poll["added_players"].values():
        come.append(f"{html.escape(player['name'])} (добавил {player['added_by_username']})")

    title = poll.get("title") or "Опрос на игру"
    is_full = count_come(poll) >= FULL_SQUAD_SIZE
    status_line = "⛔ Команды уже набраны" if is_full else "👬 Открыт набор игроков"
    lines = [f"<b>{title}</b>", status_line, ""]
    lines.append(f"✅ Приду ({len(come)}):")
    lines.extend(f"  • {n}" for n in come) if come else lines.append("  —")
    lines.append("")
    lines.append(f"❌ Пропущу ({len(skip)}):")
    lines.extend(f"  • {n}" for n in skip) if skip else lines.append("  —")
    lines.append("")
    lines.append(f"❔ Пока не уверен ({len(unsure)}):")
    lines.extend(f"  • {n}" for n in unsure) if unsure else lines.append("  —")
    return "\n".join(lines)


async def refresh_message(chat_id: int):
    poll = get_poll(chat_id)
    if poll["message_id"] is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=poll["message_id"],
            text=build_poll_text(poll),
            reply_markup=build_keyboard(poll),
        )
    except Exception as e:
        logger.warning("Не удалось обновить сообщение опроса: %s", e)

    is_full = count_come(poll) >= FULL_SQUAD_SIZE
    if is_full and not poll["announced_full"]:
        poll["announced_full"] = True
        await bot.send_message(chat_id, "🎉 Комплект!")
    elif not is_full and poll["announced_full"]:
        poll["announced_full"] = False
        await bot.send_message(chat_id, "Опять не хватает игроков ((")


# ---------------------------------------------------------------------------
# Запуск опроса
# ---------------------------------------------------------------------------

async def start_poll(chat_id: int, title: str | None = None):
    previous = polls.get(chat_id)
    if previous and previous["message_id"] is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=previous["message_id"],
                reply_markup=None,
            )
        except Exception as e:
            logger.warning("Не удалось убрать кнопки у предыдущего опроса: %s", e)

    polls[chat_id] = new_poll_state()
    poll = polls[chat_id]
    poll["title"] = title
    msg = await bot.send_message(chat_id, build_poll_text(poll), reply_markup=build_keyboard(poll))
    poll["message_id"] = msg.message_id


@dp.message(Command("poll"))
async def cmd_poll(message: Message, command: CommandObject):
    # /poll Название опроса — если название не указано, используется значение по умолчанию
    title = command.args.strip() if command.args else None
    await start_poll(message.chat.id, title)


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("Останавливать опрос может только админ.")
        return

    poll = polls.get(message.chat.id)
    if poll is None or poll["message_id"] is None:
        await message.reply("Активного опроса в этом чате нет.")
        return

    poll["closed"] = True
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=poll["message_id"],
            reply_markup=None,
        )
    except Exception as e:
        logger.warning("Не удалось убрать кнопки при остановке опроса: %s", e)

    await message.answer("Опрос остановлен, ответы больше не принимаются.", disable_notification=True)

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logger.warning("Не удалось удалить команду /stop: %s", e)


# ---------------------------------------------------------------------------
# Обработка нажатий: статус приду/пропущу/не уверен
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("status:"))
async def on_status(callback: CallbackQuery):
    poll = get_poll(callback.message.chat.id)
    if poll["closed"]:
        await callback.answer("Опрос остановлен, ответы больше не принимаются.", show_alert=True)
        return
    status = callback.data.split(":", 1)[1]
    poll["responses"][callback.from_user.id] = {
        "username": display_name(callback.from_user),
        "status": status,
    }
    await refresh_message(callback.message.chat.id)
    await callback.answer(f"Отмечено: {STATUS_LABELS[status]}")


# ---------------------------------------------------------------------------
# Добавить игрока
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "add_player")
async def on_add_player_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPlayer.waiting_name)
    await callback.answer()
    prompt = await callback.message.answer(
        f"{display_name(callback.from_user)}, напишите в ответ имя игрока, которого добавляете.",
        disable_notification=True,
    )
    await state.update_data(chat_id=callback.message.chat.id, prompt_message_id=prompt.message_id)


@dp.message(AddPlayer.waiting_name)
async def on_add_player_name(message: Message, state: FSMContext):
    global _player_id_counter
    data = await state.get_data()
    chat_id = data["chat_id"]
    prompt_message_id = data.get("prompt_message_id")
    poll = get_poll(chat_id)

    _player_id_counter += 1
    player_id = _player_id_counter
    poll["added_players"][player_id] = {
        "name": message.text.strip(),
        "added_by": message.from_user.id,
        "added_by_username": display_name(message.from_user),
    }
    await state.clear()

    # Удаляем запрос имени и ответ пользователя — новое имя и так видно в тексте опроса.
    # Для удаления боту нужны права администратора с разрешением "Удаление сообщений".
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception as e:
        logger.warning("Не удалось удалить сообщение с именем игрока: %s", e)
    if prompt_message_id:
        try:
            await bot.delete_message(chat_id, prompt_message_id)
        except Exception as e:
            logger.warning("Не удалось удалить запрос имени игрока: %s", e)

    await refresh_message(chat_id)


# ---------------------------------------------------------------------------
# Убрать добавленного игрока
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "remove_player_menu")
async def on_remove_player_menu(callback: CallbackQuery):
    poll = get_poll(callback.message.chat.id)
    user_id = callback.from_user.id
    is_admin = user_id in ADMIN_IDS

    # Игрока может убрать тот, кто его добавил, либо админ.
    visible = {
        pid: p for pid, p in poll["added_players"].items()
        if is_admin or p["added_by"] == user_id
    }

    if not visible:
        await callback.answer("Нет добавленных вами игроков для удаления.", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=f"❌ {p['name']}", callback_data=f"remove_player:{pid}")]
        for pid, p in visible.items()
    ]
    await callback.answer()
    await callback.message.answer(
        "Кого убрать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_notification=True,
    )


@dp.callback_query(F.data.startswith("remove_player:"))
async def on_remove_player(callback: CallbackQuery):
    player_id = int(callback.data.split(":", 1)[1])
    poll = get_poll(callback.message.chat.id)
    user_id = callback.from_user.id
    is_admin = user_id in ADMIN_IDS

    player = poll["added_players"].get(player_id)
    if player is None:
        await callback.answer("Этот игрок уже убран.", show_alert=True)
        return

    if not is_admin and player["added_by"] != user_id:
        await callback.answer("Убрать может только тот, кто добавил, или админ.", show_alert=True)
        return

    del poll["added_players"][player_id]
    await callback.answer(f"Игрок «{player['name']}» убран.")
    try:
        await callback.message.delete()
    except Exception as e:
        logger.warning("Не удалось удалить меню выбора игрока: %s", e)
    await refresh_message(callback.message.chat.id)


# ---------------------------------------------------------------------------
# Запуск опроса по расписанию (опционально)
# ---------------------------------------------------------------------------

async def scheduled_poll():
    if not SCHEDULE_CHAT_ID:
        logger.warning("SCHEDULE_CHAT_ID не задан — автозапуск опроса пропущен.")
        return
    await start_poll(int(SCHEDULE_CHAT_ID))


def setup_scheduler():
    if not SCHEDULE_TIME:
        return None
    hour, minute = (int(x) for x in SCHEDULE_TIME.split(":"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_poll, "cron", hour=hour, minute=minute)
    scheduler.start()
    return scheduler


# ---------------------------------------------------------------------------
# Health-check веб-сервер (нужен только для бесплатного тарифа Render Web Service —
# сам Telegram-бот работает через polling и открытый порт ему не требуется)
# ---------------------------------------------------------------------------

async def health(request):
    return web.Response(text="OK")


async def run_web_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check сервер запущен на порту %s", port)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    setup_scheduler()
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
