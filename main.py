"""
⛏ Mine Bot — игровой Telegram-бот
Версия 0.4: копка по таймеру + инвентарь + монеты, кошелёк и скупщик руды

Установка:
    pip install "aiogram>=3.7"

Запуск:
    вставь токен в BOT_TOKEN ниже и выполни:  python main.py
"""

import asyncio
import html
import logging
import math
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

DIG_TIME = 60          # сколько длится одна копка (сек)
FLOOD_INTERVAL = 0.7   # минимум между нажатиями одного игрока (сек)

# За одну копку игрок получает случайное число каждого ресурса
# в диапазоне amount = (мин, макс).
# price — цена продажи одной штуки у скупщика (в монетах).
RESOURCES: dict[str, dict] = {
    "stone": {"emoji": "🪨", "name": "Камень",  "amount": (15, 25), "price": 1},
    "coal":  {"emoji": "⚫", "name": "Уголь",   "amount": (2, 7),   "price": 5},
    "flint": {"emoji": "🔥", "name": "Кремень", "amount": (5, 12),  "price": 3},
}

# Премиум-эмодзи (работают в тексте сообщений; если не поддерживаются —
# Telegram покажет обычные 🤑 и 💰).
COIN = '<tg-emoji emoji-id="5433631991675244306">🤑</tg-emoji>'     # монета
WALLET = '<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji>'   # кошелёк

SELL_STEPS = (1, 10, 50)  # кнопки «продать N штук» (плюс «Всё»)

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
try:  # миграция для старых баз
    db.execute("ALTER TABLE users ADD COLUMN dig_end REAL NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
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
# Состояния копки (хранится время окончания dig_end):
#   dig_end == 0          → шахтёр свободен
#   сейчас < dig_end      → копает
#   сейчас >= dig_end > 0 → добыча готова, можно забрать

def dig_state(user_id: int) -> tuple[str, float]:
    """Возвращает ('idle' | 'digging' | 'ready', секунд до конца)."""
    end = get_user(user_id)["dig_end"]
    if end <= 0:
        return "idle", 0.0
    left = end - time.time()
    return ("digging", left) if left > 0 else ("ready", 0.0)


def start_dig(user_id: int) -> bool:
    """Начинает копку, если шахтёр свободен."""
    if dig_state(user_id)[0] != "idle":
        return False
    db.execute(
        "UPDATE users SET dig_end = ? WHERE user_id = ?",
        (time.time() + DIG_TIME, user_id),
    )
    db.commit()
    return True


def claim(user_id: int) -> dict[str, int] | None:
    """Забирает добычу, если копка завершена."""
    if dig_state(user_id)[0] != "ready":
        return None
    drops = {item: random.randint(*res["amount"]) for item, res in RESOURCES.items()}
    for item, amount in drops.items():
        add_item(user_id, item, amount)
    db.execute(
        "UPDATE users SET dig_end = 0, swings = swings + 1, "
        "mined_total = mined_total + ? WHERE user_id = ?",
        (sum(drops.values()), user_id),
    )
    db.commit()
    return drops


def sell(user_id: int, item: str, count: int | None) -> tuple[int, int]:
    """
    Продаёт руду. count=None — продать всё.
    Возвращает (продано штук, получено монет).
    """
    res = RESOURCES.get(item)
    if res is None:
        return 0, 0
    have = get_inventory(user_id).get(item, 0)
    n = have if count is None else min(count, have)
    if n <= 0:
        return 0, 0
    earned = n * res["price"]
    db.execute(
        "UPDATE inventory SET amount = amount - ? WHERE user_id = ? AND item = ?",
        (n, user_id, item),
    )
    db.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?", (earned, user_id)
    )
    db.commit()
    return n, earned


def sell_everything(user_id: int) -> tuple[dict[str, int], int]:
    """Продаёт все руды. Возвращает ({ресурс: штук}, всего монет)."""
    sold: dict[str, int] = {}
    total = 0
    for item in RESOURCES:
        n, earned = sell(user_id, item, None)
        if n:
            sold[item] = n
            total += earned
    return sold, total


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
        (("🎒 Инвентарь", "inventory"), ("🛒 Скупщик", "market")),
        (("👤 Профиль", "profile"),),
    )


def inventory_kb() -> InlineKeyboardMarkup:
    return kb(
        (("🛒 Продать руду", "market"),),
        (("⛏ В шахту", "mine"), ("🏠 Меню", "menu")),
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


HEADER = "⛏ <b>Каменная шахта</b>\n" + LINE + "\n"


def progress_bar(left: float) -> str:
    done = round(10 * (1 - left / DIG_TIME))
    done = max(0, min(10, done))
    return "▰" * done + "▱" * (10 - done)


def idle_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        HEADER
        + "<i>Сырые своды, капли воды и запах руды. "
        "Где-то в глубине эхом отзывается чужая кирка.</i>\n\n"
        f"⏱ Одна копка занимает <b>{DIG_TIME // 60} мин.</b>\n"
        "Нажми <b>«Копать»</b> — и шахтёр отправится за добычей."
    )
    markup = kb(
        (("⛏  Копать", "dig"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )
    return text, markup


def digging_screen(left: float) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        HEADER
        + "<i>Шахтёр орудует киркой в глубине тоннеля. "
        "Слышен глухой стук и звон породы…</i>\n\n"
        f"{progress_bar(left)}\n"
        f"⏳ Осталось: <b>{math.ceil(left)} сек.</b>"
    )
    markup = kb(
        (("🔄 Обновить", "mine"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )
    return text, markup


def ready_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        HEADER
        + "✅ <b>Копка завершена!</b>\n"
        "<i>Шахтёр вернулся с гружёной тележкой и ждёт тебя.</i>\n\n"
        f"{progress_bar(0)}"
    )
    markup = kb(
        (("📦 Забрать добычу", "claim"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )
    return text, markup


def result_screen(drops: dict[str, int]) -> tuple[str, InlineKeyboardMarkup]:
    loot = "\n".join(
        f"{RESOURCES[i]['emoji']} {RESOURCES[i]['name']} <b>×{n}</b>"
        for i, n in drops.items()
    )
    text = (
        HEADER
        + "🎁 <b>Добыча получена!</b>\n"
        f"<blockquote>{loot}</blockquote>\n"
        "<i>Всё сложено в рюкзак. Можно отправляться снова.</i>"
    )
    markup = kb(
        (("⛏  Копать снова", "dig"),),
        (("🎒 Инвентарь", "inventory"), ("🏠 Меню", "menu")),
    )
    return text, markup


def mine_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран шахты в зависимости от состояния копки."""
    state, left = dig_state(user_id)
    if state == "digging":
        return digging_screen(left)
    if state == "ready":
        return ready_screen()
    return idle_screen()


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
        f"📦 Всего предметов: <b>{total}</b>\n"
        f"{WALLET} Кошелёк: <b>{get_user(user_id)['balance']}</b> {COIN}\n\n"
        "<i>Всё, что добыто честным трудом, лежит здесь.</i>"
    )


def profile_text(user_id: int) -> str:
    u = get_user(user_id)
    return (
        "👤 <b>Профиль шахтёра</b>\n"
        f"{LINE}\n"
        f"🏷 Имя: <b>{html.escape(u['name'])}</b>\n"
        f"{WALLET} Кошелёк: <b>{u['balance']}</b> {COIN}\n"
        f"⛏ Копок завершено: <b>{u['swings']}</b>\n"
        f"💎 Добыто ресурсов: <b>{u['mined_total']}</b>\n\n"
        "<i>Каждый великий шахтёр начинал с одной копки.</i>"
    )


def market_screen(user_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Главный экран скупщика: цены, запасы, выбор руды."""
    inv = get_inventory(user_id)
    lines = [
        f"{res['emoji']} {res['name']} — <b>{inv.get(item, 0)}</b> шт. "
        f"<i>(по {res['price']} {COIN})</i>"
        for item, res in RESOURCES.items()
    ]
    worth = sum(inv.get(i, 0) * r["price"] for i, r in RESOURCES.items())
    text = (
        "🛒 <b>Скупщик руды</b>\n"
        f"{LINE}\n"
        "<i>Старый гном щёлкает счётами и придирчиво разглядывает "
        "твой мешок: «Хорошую цену дам, не обижу!»</i>\n\n"
    )
    if note:
        text += f"{note}\n\n"
    text += (
        "<blockquote>" + "\n".join(lines) + "</blockquote>\n"
        f"{WALLET} Кошелёк: <b>{get_user(user_id)['balance']}</b> {COIN}\n"
        f"💼 Всё можно продать за: <b>{worth}</b> {COIN}"
    )
    rows: list[tuple[tuple[str, str], ...]] = [
        tuple((f"{r['emoji']} {r['name']}", f"sell_menu:{i}") for i, r in RESOURCES.items())
    ]
    if worth:
        rows.append((("💸 Продать всё", "sell_all"),))
    rows.append((("⛏ В шахту", "mine"), ("🏠 Меню", "menu")))
    return text, kb(*rows)


def sell_screen(user_id: int, item: str, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Экран продажи одной руды."""
    res = RESOURCES[item]
    have = get_inventory(user_id).get(item, 0)
    text = (
        f"🛒 <b>Продажа: {res['emoji']} {res['name']}</b>\n"
        f"{LINE}\n"
    )
    if note:
        text += f"{note}\n\n"
    text += (
        f"🎒 У тебя: <b>{have}</b> шт.\n"
        f"🏷 Цена: <b>{res['price']}</b> {COIN} за штуку\n"
        f"{WALLET} Кошелёк: <b>{get_user(user_id)['balance']}</b> {COIN}\n\n"
        "<i>Сколько отсыпать гному?</i>"
    )
    step_row = tuple((f"×{n}", f"sell:{item}:{n}") for n in SELL_STEPS) + (
        ("Всё", f"sell:{item}:all"),
    )
    return text, kb(step_row, (("🛒 Назад к скупщику", "market"),))


# ══════════════════════════════════════════════════════════════
#  ЗАЩИТА ОТ ФЛУДА / ЛИМИТОВ TELEGRAM
# ══════════════════════════════════════════════════════════════
#   1) Middleware пропускает не чаще 1 действия за FLOOD_INTERVAL
#      на игрока — лишние нажатия гасятся молча.
#   2) Новых сообщений не шлём — только редактируем текущее.
#   3) На каждую кнопку сразу отвечаем call.answer().
#   4) Копка идёт по таймеру: игрок не кликает, значит запросов мало.
#      По окончании бот сам один раз обновляет сообщение.
#   5) При TelegramRetryAfter не повторяем запрос, а сообщаем игроку.


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
_tasks: set[asyncio.Task] = set()  # чтобы задачи не удалялись сборщиком мусора


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


async def notify_when_ready(bot: Bot, user_id: int, chat_id: int, message_id: int) -> None:
    """Через DIG_TIME сек один раз обновляет сообщение: «Копка завершена»."""
    await asyncio.sleep(DIG_TIME + 0.5)
    try:
        if dig_state(user_id)[0] != "ready":  # уже забрал или что-то изменилось
            return
        text, markup = ready_screen()
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
    except (TelegramBadRequest, TelegramRetryAfter):
        pass  # не страшно: игрок увидит результат по кнопке «Обновить»
    except Exception:
        logging.exception("Ошибка в notify_when_ready")


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
    await show(call, *mine_screen(call.from_user.id))


@router.callback_query(F.data == "dig")
async def cb_dig(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user(uid, call.from_user.full_name)

    if start_dig(uid):
        task = asyncio.create_task(
            notify_when_ready(call.bot, uid, call.message.chat.id, call.message.message_id)
        )
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    await show(call, *mine_screen(uid))


@router.callback_query(F.data == "claim")
async def cb_claim(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user(uid, call.from_user.full_name)
    drops = claim(uid)
    if drops is None:
        await show(call, *mine_screen(uid))  # ещё копает или уже забрано
        return
    await show(call, *result_screen(drops))


@router.callback_query(F.data == "inventory")
async def cb_inventory(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, inventory_text(call.from_user.id), inventory_kb())


@router.callback_query(F.data == "market")
async def cb_market(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    await show(call, *market_screen(call.from_user.id))


@router.callback_query(F.data.startswith("sell_menu:"))
async def cb_sell_menu(call: CallbackQuery) -> None:
    ensure_user(call.from_user.id, call.from_user.full_name)
    item = call.data.split(":")[1]
    if item not in RESOURCES:
        await call.answer()
        return
    await show(call, *sell_screen(call.from_user.id, item))


@router.callback_query(F.data.startswith("sell:"))
async def cb_sell(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user(uid, call.from_user.full_name)
    try:
        _, item, amount = call.data.split(":")
        count = None if amount == "all" else int(amount)
    except ValueError:
        await call.answer()
        return
    if item not in RESOURCES:
        await call.answer()
        return

    n, earned = sell(uid, item, count)
    res = RESOURCES[item]
    if n:
        note = (
            f"✅ Продано: {res['emoji']} {res['name']} <b>×{n}</b>\n"
            f"💵 Получено: <b>+{earned}</b> {COIN}"
        )
    else:
        note = f"❌ <i>Нечего продавать — {res['name'].lower()} закончился.</i>"
    await show(call, *sell_screen(uid, item, note))


@router.callback_query(F.data == "sell_all")
async def cb_sell_all(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user(uid, call.from_user.full_name)
    sold, total = sell_everything(uid)
    if sold:
        items = "\n".join(
            f"{RESOURCES[i]['emoji']} {RESOURCES[i]['name']} <b>×{n}</b>"
            for i, n in sold.items()
        )
        note = (
            f"✅ <b>Всё продано!</b>\n<blockquote>{items}</blockquote>\n"
            f"💵 Получено: <b>+{total}</b> {COIN}"
        )
    else:
        note = "❌ <i>Продавать пока нечего — сначала накопай руды.</i>"
    await show(call, *market_screen(uid, note))


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
