"""
⛏ Mine Bot — игровой Telegram-бот
Версия 0.1: каменная шахта + инвентарь

Установка:
    pip install "aiogram>=3.7"

Запуск:
    вставь токен в переменную BOT_TOKEN ниже в настройках
    python main.py
"""

import asyncio
import html
import logging
import os
import random
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════

BOT_TOKEN = "8771626322:AAFFJJimbIq4JLTh4-uHNkHDHcUCXiUn3fY"  # токен от @BotFather
DB_PATH = "game.db"
MINE_COOLDOWN = 2.0  # секунд между ударами киркой

# Ресурсы: id -> (эмодзи, название, шанс выпадения, (мин, макс) количество)
RESOURCES: dict[str, dict] = {
    "stone": {"emoji": "🪨", "name": "Камень",  "weight": 60, "amount": (1, 3)},
    "flint": {"emoji": "🔥", "name": "Кремень", "weight": 25, "amount": (1, 2)},
    "coal":  {"emoji": "⚫", "name": "Уголь",   "weight": 15, "amount": (1, 2)},
}

# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        name        TEXT    NOT NULL,
        mined_total INTEGER NOT NULL DEFAULT 0,
        swings      INTEGER NOT NULL DEFAULT 0,
        last_mine   REAL    NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS inventory (
        user_id INTEGER NOT NULL,
        item    TEXT    NOT NULL,
        amount  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, item)
    );
    """
)
db.commit()


def ensure_user(user_id: int, name: str) -> None:
    db.execute(
        "INSERT INTO users (user_id, name) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
        (user_id, name),
    )
    db.commit()


def get_user(user_id: int) -> sqlite3.Row:
    return db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_inventory(user_id: int) -> dict[str, int]:
    rows = db.execute(
        "SELECT item, amount FROM inventory WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["item"]: r["amount"] for r in rows}


def add_item(user_id: int, item: str, amount: int) -> None:
    db.execute(
        "INSERT INTO inventory (user_id, item, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, item) DO UPDATE SET amount = amount + excluded.amount",
        (user_id, item, amount),
    )


# ══════════════════════════════════════════════════════════════
#  ИГРОВАЯ ЛОГИКА
# ══════════════════════════════════════════════════════════════

def roll_drop() -> tuple[str, int]:
    """Случайный ресурс и его количество."""
    ids = list(RESOURCES)
    weights = [RESOURCES[i]["weight"] for i in ids]
    item = random.choices(ids, weights=weights, k=1)[0]
    low, high = RESOURCES[item]["amount"]
    return item, random.randint(low, high)


def mine(user_id: int) -> tuple[str, int] | float:
    """
    Удар киркой. Возвращает (ресурс, количество)
    или число секунд, которые осталось ждать.
    """
    user = get_user(user_id)
    now = time.time()
    wait = MINE_COOLDOWN - (now - user["last_mine"])
    if wait > 0:
        return wait

    item, amount = roll_drop()
    add_item(user_id, item, amount)
    db.execute(
        "UPDATE users SET mined_total = mined_total + ?, swings = swings + 1, "
        "last_mine = ? WHERE user_id = ?",
        (amount, now, user_id),
    )
    db.commit()
    return item, amount


# ══════════════════════════════════════════════════════════════
#  ТЕКСТЫ И КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════

LINE = "━━━━━━━━━━━━━━━━━━"


def kb(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    """Быстрая сборка клавиатуры: каждая строка — кортеж (текст, callback)."""
    builder = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            builder.button(text=text, callback_data=data)
    builder.adjust(*(len(r) for r in rows))
    return builder.as_markup()


def menu_kb() -> InlineKeyboardMarkup:
    return kb(
        (("⛏ Шахта", "mine"),),
        (("🎒 Инвентарь", "inventory"), ("👤 Профиль", "profile")),
    )


def mine_kb() -> InlineKeyboardMarkup:
    return kb(
        (("⛏ Добывать", "swing"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )


def back_kb() -> InlineKeyboardMarkup:
    return kb((("⛏ В шахту", "mine"), ("🏠 Меню", "menu")))


def menu_text(name: str) -> str:
    return (
        f"🏠 <b>Главное меню</b>\n{LINE}\n"
        f"Привет, <b>{html.escape(name)}</b>!\n"
        f"Добро пожаловать в мир шахтёров.\n\n"
        f"Выбери, куда отправиться 👇"
    )


def mine_text(last: str = "") -> str:
    text = (
        f"⛏ <b>Каменная шахта</b>\n{LINE}\n"
        f"Тёмные своды, эхо капель и запах руды.\n"
        f"Здесь можно найти: "
        + ", ".join(f"{r['emoji']} {r['name']}" for r in RESOURCES.values())
        + "\n"
    )
    if last:
        text += f"\n{last}\n"
    text += "\nЖми <b>«Добывать»</b> и бей киркой!"
    return text


def inventory_text(user_id: int) -> str:
    inv = get_inventory(user_id)
    lines = []
    for item, res in RESOURCES.items():
        lines.append(f"{res['emoji']} {res['name']:<8} — <b>{inv.get(item, 0)}</b>")
    total = sum(inv.values())
    return (
        f"🎒 <b>Инвентарь</b>\n{LINE}\n"
        + "\n".join(lines)
        + f"\n{LINE}\n📦 Всего предметов: <b>{total}</b>"
    )


def profile_text(user_id: int) -> str:
    u = get_user(user_id)
    return (
        f"👤 <b>Профиль</b>\n{LINE}\n"
        f"Имя: <b>{html.escape(u['name'])}</b>\n"
        f"⛏ Ударов киркой: <b>{u['swings']}</b>\n"
        f"💎 Добыто ресурсов: <b>{u['mined_total']}</b>"
    )


# ══════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

router = Router()


async def show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Редактирует сообщение (без спама новыми)."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    user = message.from_user
    ensure_user(user.id, user.full_name)
    await message.answer(menu_text(user.full_name), reply_markup=menu_kb())


@router.message(Command("inventory"))
async def cmd_inventory(message: Message) -> None:
    ensure_user(message.from_user.id, message.from_user.full_name)
    await message.answer(inventory_text(message.from_user.id), reply_markup=back_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, menu_text(call.from_user.full_name), menu_kb())
    await call.answer()


@router.callback_query(F.data == "mine")
async def cb_mine(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, mine_text(), mine_kb())
    await call.answer()


@router.callback_query(F.data == "swing")
async def cb_swing(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    result = mine(call.from_user.id)

    if isinstance(result, float):
        await call.answer(f"⏳ Кирка ещё качается... {result:.1f} сек", show_alert=False)
        return

    item, amount = result
    res = RESOURCES[item]
    last = f"✅ Добыто: {res['emoji']} <b>{res['name']}</b> ×{amount}"
    await show(call, mine_text(last), mine_kb())
    await call.answer()


@router.callback_query(F.data == "inventory")
async def cb_inventory(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, inventory_text(call.from_user.id), back_kb())
    await call.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, profile_text(call.from_user.id), back_kb())
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН":
        raise SystemExit("❌ Вставь токен бота в переменную BOT_TOKEN в main.py")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("⛏ Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
