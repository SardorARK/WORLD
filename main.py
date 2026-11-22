"""
⛏ Mine Bot — игровой Telegram-бот
Версия 0.2: каменная шахта + инвентарь + защита от флуда

Установка:
    pip install "aiogram>=3.7"

Запуск:
    вставь токен в BOT_TOKEN ниже и выполни:  python main.py
"""

import asyncio
import html
import logging
import random
import sqlite3
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════

BOT_TOKEN = "8806693883:AAHRStDHxDVzlgY72cQamX7GhNf2xY7J8R0"  # токен от @BotFather
DB_PATH = "game.db"

MINE_COOLDOWN = 1.5      # пауза между ударами киркой (сек)
FLOOD_INTERVAL = 0.7     # минимум между любыми нажатиями одного игрока (сек)

# Каждый удар кидает отдельный шанс на каждый ресурс.
# chance — вероятность выпадения, amount — (мин, макс) штук.
RESOURCES: dict[str, dict] = {
    "stone": {"emoji": "🪨", "name": "Камень",  "chance": 0.70, "amount": (1, 2)},
    "coal":  {"emoji": "⚫",  "name": "Уголь",   "chance": 0.30, "amount": (1, 1)},
    "flint": {"emoji": "🔥", "name": "Кремень", "chance": 0.30, "amount": (1, 2)},
}

# Разные фразы — заодно делают текст каждый раз новым,
# поэтому Telegram не ругается «message is not modified».
SWING_PHRASES = [
    "Кирка со звоном врезается в породу…",
    "Эхо удара катится по тёмным тоннелям…",
    "Из стены сыплется каменная крошка…",
    "Порода поддаётся с глухим треском…",
    "Искры летят из-под острия кирки…",
    "Ты вытираешь пот и бьёшь снова…",
    "Лампа мигает, а стена трещит по швам…",
    "Пыль оседает — что-то блеснуло в темноте…",
]

EMPTY_PHRASES = [
    "Порода оказалась пустой — ничего не нашлось.",
    "Удар ушёл впустую, только пыль в лицо.",
    "Камень раскололся, но внутри ничего ценного.",
]

LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

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

def roll_drops() -> dict[str, int]:
    """Камень 70% (1–2), уголь 30% (1), кремень 30% (1–2). Удар может выйти пустым."""
    drops: dict[str, int] = {}
    for item, res in RESOURCES.items():
        if random.random() < res["chance"]:
            drops[item] = random.randint(*res["amount"])
    return drops


def mine(user_id: int) -> dict[str, int] | float:
    """Удар киркой. Возвращает добычу или число секунд ожидания."""
    user = get_user(user_id)
    now = time.time()
    wait = MINE_COOLDOWN - (now - user["last_mine"])
    if wait > 0:
        return wait

    drops = roll_drops()
    for item, amount in drops.items():
        add_item(user_id, item, amount)
    db.execute(
        "UPDATE users SET mined_total = mined_total + ?, swings = swings + 1, "
        "last_mine = ? WHERE user_id = ?",
        (sum(drops.values()), now, user_id),
    )
    db.commit()
    return drops


# ══════════════════════════════════════════════════════════════
#  ТЕКСТЫ И КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════

def kb(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    """Каждая строка — кортеж кнопок (текст, callback_data)."""
    builder = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            builder.button(text=text, callback_data=data)
    builder.adjust(*(len(r) for r in rows))
    return builder.as_markup()


def menu_kb() -> InlineKeyboardMarkup:
    return kb(
        (("⛏  Спуститься в шахту", "mine"),),
        (("🎒 Инвентарь", "inventory"), ("👤 Профиль", "profile")),
    )


def mine_kb() -> InlineKeyboardMarkup:
    return kb(
        (("⛏  Ударить киркой", "swing"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )


def back_kb() -> InlineKeyboardMarkup:
    return kb((("⛏ В шахту", "mine"), ("🏠 Меню", "menu")))


def menu_text(name: str) -> str:
    return (
        "🏔 <b>Горняцкий посёлок</b>\n"
        f"{LINE}\n"
        f"Добро пожаловать, <b>{html.escape(name)}</b>!\n\n"
        "<i>Ветер гонит пыль по улочкам, а у входа в шахту "
        "тускло горит фонарь. Кирка ждёт своего хозяина…</i>\n\n"
        "👇 <b>Куда отправимся?</b>"
    )


def mine_text(last_drops: dict[str, int] | None = None) -> str:
    text = (
        "⛏ <b>Каменная шахта</b>\n"
        f"{LINE}\n"
        "<i>Сырые своды, капли воды и запах руды. "
        "Где-то в глубине эхом отзывается чужая кирка.</i>\n\n"
        "🔎 <b>Можно найти:</b>\n"
        + "\n".join(
            f"   {r['emoji']} {r['name']}" for r in RESOURCES.values()
        )
        + "\n"
    )
    if last_drops is not None:
        text += f"\n💥 <b>Удар!</b>\n<i>{random.choice(SWING_PHRASES)}</i>\n\n"
        if last_drops:
            text += (
                "🎁 <b>Добыча:</b>\n<blockquote>"
                + "\n".join(
                    f"{RESOURCES[i]['emoji']} {RESOURCES[i]['name']} <b>×{n}</b>"
                    for i, n in last_drops.items()
                )
                + "</blockquote>"
            )
        else:
            text += f"💨 <i>{random.choice(EMPTY_PHRASES)}</i>"
    else:
        text += "\n<i>Нажми на кнопку и ударь по породе…</i>"
    return text


def inventory_text(user_id: int) -> str:
    inv = get_inventory(user_id)
    lines = [
        f"{res['emoji']} {res['name']} — <b>{inv.get(item, 0)}</b>"
        for item, res in RESOURCES.items()
    ]
    total = sum(inv.values())
    return (
        "🎒 <b>Твой инвентарь</b>\n"
        f"{LINE}\n"
        "<blockquote>" + "\n".join(lines) + "</blockquote>\n"
        f"📦 Всего предметов: <b>{total}</b>\n\n"
        "<i>Всё, что добыто честным трудом, лежит здесь.</i>"
    )


def profile_text(user_id: int) -> str:
    u = get_user(user_id)
    return (
        "👤 <b>Профиль шахтёра</b>\n"
        f"{LINE}\n"
        f"🏷 Имя: <b>{html.escape(u['name'])}</b>\n"
        f"⛏ Ударов киркой: <b>{u['swings']}</b>\n"
        f"💎 Добыто ресурсов: <b>{u['mined_total']}</b>\n\n"
        "<i>Каждый великий шахтёр начинал с одного удара.</i>"
    )


# ══════════════════════════════════════════════════════════════
#  ЗАЩИТА ОТ ФЛУДА / ЛИМИТОВ TELEGRAM
# ══════════════════════════════════════════════════════════════
# Telegram ограничивает частоту запросов бота (примерно 1 правка
# одного сообщения в секунду). Что делаем, чтобы не словить 429:
#   1) Middleware пропускает не чаще 1 действия за FLOOD_INTERVAL
#      на игрока — лишние нажатия гасятся молча.
#   2) Новых сообщений не шлём — только редактируем текущее.
#   3) На любой нажатой кнопке сразу отвечаем call.answer().
#   4) При TelegramRetryAfter не долбим повторно, а ждём.


class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            now = time.monotonic()
            if now - self.last.get(user.id, 0) < self.interval:
                if isinstance(event, CallbackQuery):
                    await event.answer()  # тихо гасим «часики» на кнопке
                return None
            self.last[user.id] = now
            if len(self.last) > 10_000:  # чистим память
                cutoff = now - 60
                self.last = {k: v for k, v in self.last.items() if v > cutoff}
        return await handler(event, data)


# ══════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

router = Router()


async def show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Редактирует сообщение, не падая на лимитах Telegram."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
        await call.answer()
    except TelegramRetryAfter as e:
        logging.warning("Flood control: ждём %s сек", e.retry_after)
        await call.answer(f"⏳ Не так быстро! Подожди {e.retry_after} сек.")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await call.answer()
        elif "message to edit not found" in str(e) or "can't be edited" in str(e):
            await call.message.answer(text, reply_markup=markup)
            await call.answer()
        else:
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


@router.callback_query(F.data == "mine")
async def cb_mine(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, mine_text(), mine_kb())


@router.callback_query(F.data == "swing")
async def cb_swing(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    result = mine(call.from_user.id)

    if isinstance(result, float):
        await call.answer(f"⏳ Кирка ещё звенит… {result:.1f} сек")
        return

    await show(call, mine_text(result), mine_kb())


@router.callback_query(F.data == "inventory")
async def cb_inventory(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, inventory_text(call.from_user.id), back_kb())


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, profile_text(call.from_user.id), back_kb())


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН":
        raise SystemExit("❌ Вставь токен бота в переменную BOT_TOKEN в main.py")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(AntiFloodMiddleware(FLOOD_INTERVAL))
    dp.callback_query.middleware(AntiFloodMiddleware(FLOOD_INTERVAL))
    dp.include_router(router)

    logging.info("⛏ Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
