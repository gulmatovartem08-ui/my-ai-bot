import os
import asyncio
import logging
import aiosqlite
import random
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("AI_KEY")
OWNER_USERNAME = "flaybbe"
DB_PATH = "database.db"
FREE_LIMIT = 2

# ============ ИНИЦИАЛИЗАЦИЯ ============
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)
ai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

OWNER_ID = None
PENDING_REPORTS = {}

# ============ БАЗА ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                premium_until TEXT,
                balance INTEGER DEFAULT 0,
                messages INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item TEXT,
                price INTEGER,
                date TEXT
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

async def create_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?,?,?,?)",
            (user_id, username, first_name, datetime.now().isoformat())
        )
        await db.commit()

async def is_premium(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user or not user["premium_until"]:
        return False
    if user["premium_until"] == "forever":
        return True
    try:
        return datetime.fromisoformat(user["premium_until"]) > datetime.now()
    except:
        return False

async def set_premium(user_id: int, days):
    if days == "forever":
        value = "forever"
    else:
        value = (datetime.now() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (value, user_id))
        await db.commit()

async def remove_premium(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_until=NULL WHERE user_id=?", (user_id,))
        await db.commit()

async def inc_messages(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET messages = messages + 1 WHERE user_id=?", (user_id,))
        await db.commit()

async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def get_balance(user_id: int) -> int:
    user = await get_user(user_id)
    return user["balance"] if user else 0

# ============ КЛАВИАТУРЫ ============
def main_keyboard(user_id: int):
    rows = [
        [KeyboardButton(text="🤖 Начать чат с ИИ")],
        [KeyboardButton(text="📊 Мои статы"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="ℹ️ Инфо")],
        [KeyboardButton(text="🆔 Мой ЮЗ")],
    ]
    if OWNER_ID and user_id == OWNER_ID:
        rows.append([KeyboardButton(text="/admin")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней — 5₽", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text="30 дней — 15₽", callback_data="buy_premium_30")],
        [InlineKeyboardButton(text="30 дней (пакет) — 50₽", callback_data="buy_premium_30x")],
        [InlineKeyboardButton(text="Год — 150₽", callback_data="buy_premium_365")],
        [InlineKeyboardButton(text="Навсегда — 1488₽", callback_data="buy_premium_forever")],
        [InlineKeyboardButton(text="✍️ Написать @flaybbe", url="https://t.me/flaybbe")],
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выдать премиум", callback_data="admin_give")],
        [InlineKeyboardButton(text="❌ Забрать премиум", callback_data="admin_take")],
        [InlineKeyboardButton(text="💰 Выдать валюту (/pay)", callback_data="admin_pay")],
        [InlineKeyboardButton(text="👥 Все пользователи (/tab)", callback_data="admin_tab")],
    ])

# ============ УТИЛИТЫ ============
def is_owner(message: Message) -> bool:
    return message.from_user.username and message.from_user.username.lower() == OWNER_USERNAME.lower()

# ============ ЦЕНЗУРА ============
BANNED_WORDS = ["хуй", "пизд", "блят", "ебан", "сука", "нахуй", "fuck", "shit", "bitch", "nigger"]

def censor(text: str):
    low = text.lower()
    for w in BANNED_WORDS:
        if w in low:
            return None
    return text

# ============ ИИ ============
SYSTEM_PROMPT = """Ты — Jungle AI, умный и дружелюбный ассистент.
Ты помогаешь с кодом, объясняешь темы, пишешь тексты, генерируешь данные по запросу.
Отвечай кратко и по делу. Не упоминай других ассистентов и не называй себя иначе как Jungle AI.
Отвечай на языке пользователя."""

async def ask_ai(user_id: int, text: str) -> str:
    try:
        resp = await ai_client.chat.completions.create(
            model="gemini-2.0-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ],
            max_tokens=800,
            temperature=0.7
        )
        return resp.choices[0].message.content
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "⚠️ Ошибка ИИ. Попробуйте позже."

# ============ ГЕНЕРАЦИЯ ДАННЫХ ============
FIRST_NAMES = ["Александр", "Дмитрий", "Иван", "Максим", "Сергей", "Андрей", "Никита", "Артём"]
LAST_NAMES = ["Иванов", "Петров", "Смирнов", "Кузнецов", "Соколов", "Попов", "Лебедев"]
CITIES = ["Москва", "Санкт-Петербург", "Новосибирск", "Казань", "Екатеринбург", "Краснодар"]

def gen_identity() -> str:
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    city = random.choice(CITIES)
    year = random.randint(1980, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    phone = f"+79{random.randint(100000000, 999999999)}"
    return (
        f"🆔 <b>ЮЗ</b>\n\n"
        f"👤 ФИО: {ln} {fn}\n"
        f"📅 Дата рождения: {day:02d}.{month:02d}.{year}\n"
        f"🏙 Город: {city}\n"
        f"📱 Телефон: {phone}\n"
        f"💳 Карта: {random.randint(4000,4999)} {random.randint(1000,9999)} {random.randint(1000,9999)} {random.randint(1000,9999)}\n"
        f"📧 Email: {fn.lower()}.{ln.lower()}{random.randint(1,99)}@mail.ru"
    )

# ============ СТАРТ ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    global OWNER_ID
    u = message.from_user
    await create_user(u.id, u.username, u.first_name)
    if u.username and u.username.lower() == OWNER_USERNAME.lower():
        OWNER_ID = u.id

    await message.answer(
        "👋 Привет, я <b>Jungle AI</b>!\n\n"
        "📋 <b>Список команд:</b>\n"
        "/start — начать\n"
        "/help — репорт / помощь\n"
        "/premium — купить премиум\n"
        "/info — информация о боте\n"
        "/stats — мои сообщения\n"
        "/dok1 — купить ЮЗ\n"
        "/admin — панель владельца\n\n"
        "Просто напиши мне сообщение — и я отвечу! 🤖",
        reply_markup=main_keyboard(u.id)
    )

# ============ HELP / РЕПОРТ ============
@router.message(Command("help"))
async def cmd_help(message: Message):
    PENDING_REPORTS[message.from_user.id] = True
    await message.answer(
        "📝 <b>Репорт / Обращение</b>\n\n"
        "Напиши своё обращение владельцу — оно будет отправлено @flaybbe.\n"
        "Например: жалоба, идея, вопрос по боту.\n\n"
        "✍️ Отправь текст следующим сообщением.\n"
        "Отмена — /cancel"
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if message.from_user.id in PENDING_REPORTS:
        PENDING_REPORTS.pop(message.from_user.id)
        await message.answer("❌ Отправка репорта отменена.")
    else:
        await message.answer("Нечего отменять.")

# ============ INFO ============
@router.message(Command("info"))
async def cmd_info(message: Message):
    await message.answer(
        "ℹ️ <b>Информация о боте</b>\n\n"
        "🤖 Имя: Jungle AI\n"
        "📅 Дата создания: 2026\n"
        "🔢 Версия: 1.0.0\n"
        "👑 Создатель: @flaybbe\n"
        "📢 Канал: @jangleaikingf"
    )

# ============ PREMIUM ============
@router.message(Command("premium"))
@router.message(F.text == "💎 Премиум")
async def cmd_premium(message: Message):
    await message.answer(
        "💎 <b>Premium Jungle AI</b>\n\n"
        "Безлимитные сообщения ИИ + приоритет.\n\n"
        "💰 <b>Тарифы:</b>\n"
        "• 7 дней — 5₽\n"
        "• 30 дней — 15₽\n"
        "• 30 дней (пакет) — 50₽\n"
        "• Год — 150₽\n"
        "• Навсегда — 1488₽\n\n"
        "✍️ Для покупки напиши: @flaybbe",
        reply_markup=premium_keyboard()
    )

@router.callback_query(F.data.startswith("buy_premium_"))
async def buy_premium(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("✍️ Для оплаты напиши: @flaybbe")

# ============ STATS ============
@router.message(Command("stats"))
@router.message(F.text == "📊 Мои статы")
async def cmd_stats(message: Message):
    u = await get_user(message.from_user.id)
    if not u:
        await message.answer("Нет данных.")
        return
    prem = "✅ Активен" if await is_premium(message.from_user.id) else "❌ Нет"
    await message.answer(
        f"📊 <b>Твоя статистика</b>\n\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"👤 ЮЗ: @{u['username'] or 'нет'}\n"
        f"💬 Сообщений: <b>{u['messages']}</b>\n"
        f"💰 Баланс: <b>{u['balance']}₽</b>\n"
        f"💎 Премиум: {prem}"
    )

# ============ TOP ============
@router.message(Command("top"))
@router.message(F.text == "🏆 Топ")
async def cmd_top(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT username, first_name, messages FROM users ORDER BY messages DESC LIMIT 10") as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("Пока пусто.")
        return
    text = "🏆 <b>Топ-10 по сообщениям:</b>\n\n"
    for i, r in enumerate(rows, 1):
        name = f"@{r['username']}" if r['username'] else r['first_name']
        text += f"{i}. {name} — <b>{r['messages']}</b>\n"
    await message.answer(text)

# ============ PAY ============
@router.message(Command("pay"))
async def cmd_pay(message: Message):
    if not is_owner(message):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Формат: /pay @user 100")
        return
    target = args[1].lstrip("@")
    try:
        amount = int(args[2])
    except:
        await message.answer("Сумма — число.")
        return
    if not 1 <= amount <= 500000:
        await message.answer("Сумма от 1 до 500000.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (target.lower(),)) as cur:
            row = await cur.fetchone()
    if not row:
        await message.answer("Юзер не найден.")
        return
    await add_balance(row["user_id"], amount)
    await message.answer(f"✅ Выдано {amount}₽ @{target}")

# ============ TAB ============
@router.message(Command("tab"))
async def cmd_tab(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, username, first_name, messages, balance, premium_until FROM users ORDER BY user_id DESC LIMIT 50") as cur:
            rows = await cur.fetchall()
    text = "👥 <b>Пользователи (последние 50):</b>\n\n"
    for r in rows:
        name = f"@{r['username']}" if r['username'] else r['first_name']
        prem = "💎" if r['premium_until'] else ""
        text += f"<code>{r['user_id']}</code> | {name} | {r['messages']}см {prem}\n"
    await message.answer(text)

# ============ DOK1 ============
@router.message(Command("dok1"))
async def cmd_dok1(message: Message):
    price = 2600
    bal = await get_balance(message.from_user.id)
    if bal < price:
        await message.answer(
            f"💰 <b>ЮЗ</b>\n\n"
            f"Стоимость: <b>{price}₽</b>\n"
            f"Твой баланс: {bal}₽\n\n"
            f"Пополни баланс через @flaybbe"
        )
        return
    await add_balance(message.from_user.id, -price)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases (user_id, item, price, date) VALUES (?,?,?,?)",
                         (message.from_user.id, "ЮЗ", price, datetime.now().isoformat()))
        await db.commit()
    identity = gen_identity()
    await message.answer(identity + f"\n\n💸 Оплачено: {price}₽")

# ============ АДМИНКА ============
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    global OWNER_ID
    if not is_owner(message):
        return
    OWNER_ID = message.from_user.id
    await message.answer(
        "👑 <b>Админ-панель Jungle AI</b>\n\n"
        "Выбери действие:",
        reply_markup=admin_keyboard()
    )

@router.callback_query(F.data == "admin_give")
async def admin_give(cb: CallbackQuery):
    if not cb.from_user.username or cb.from_user.username.lower() != OWNER_USERNAME.lower():
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.answer("Введи: /giveprem @user <7|30|365|forever>")
    await cb.answer()

@router.callback_query(F.data == "admin_take")
async def admin_take(cb: CallbackQuery):
    if not cb.from_user.username or cb.from_user.username.lower() != OWNER_USERNAME.lower():
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.answer("Введи: /takeprem @user")
    await cb.answer()

@router.callback_query(F.data == "admin_pay")
async def admin_pay_cb(cb: CallbackQuery):
    await cb.message.answer("Введи: /pay @user <1..500000>")
    await cb.answer()

@router.callback_query(F.data == "admin_tab")
async def admin_tab_cb(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, username, first_name, messages, balance FROM users ORDER BY user_id DESC LIMIT 50") as cur:
            rows = await cur.fetchall()
    text = "👥 <b>Пользователи:</b>\n\n"
    for r in rows:
        name = f"@{r['username']}" if r['username'] else r['first_name']
        text += f"<code>{r['user_id']}</code> | {name} | {r['messages']} | {r['balance']}₽\n"
    await cb.message.answer(text)
    await cb.answer()

@router.message(Command("giveprem"))
async def give_premium(message: Message):
    if not is_owner(message):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Формат: /giveprem @user <7|30|365|forever>")
        return
    target = args[1].lstrip("@")
    dur = args[2]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (target.lower(),)) as cur:
            row = await cur.fetchone()
    if not row:
        await message.answer("Юзер не найден.")
        return
    if dur == "forever":
        await set_premium(row["user_id"], "forever")
    else:
        try:
            days = int(dur)
        except:
            await message.answer("Дни: 7, 30, 365 или forever")
            return
        await set_premium(row["user_id"], days)
    await message.answer(f"✅ Премиум выдан @{target} на {dur}")

@router.message(Command("takeprem"))
async def take_premium(message: Message):
    if not is_owner(message):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Формат: /takeprem @user")
        return
    target = args[1].lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (target.lower(),)) as cur:
            row = await cur.fetchone()
    if not row:
        await message.answer("Юзер не найден.")
        return
    await remove_premium(row["user_id"])
    await message.answer(f"❌ Премиум забран у @{target}")

# ============ КНОПКИ ============
@router.message(F.text == "🆔 Мой ЮЗ")
async def my_username(message: Message):
    u = message.from_user
    await message.answer(
        f"🆔 <b>Твой ЮЗ</b>\n\n"
        f"ID: <code>{u.id}</code>\n"
        f"Username: @{u.username or 'нет'}\n"
        f"Имя: {u.first_name}"
    )

@router.message(F.text == "ℹ️ Инфо")
async def info_btn(message: Message):
    await cmd_info(message)

@router.message(F.text == "🤖 Начать чат с ИИ")
async def start_ai(message: Message):
    await message.answer("🤖 Просто напиши мне сообщение — и я отвечу!")

# ============ ОБРАБОТКА РЕПОРТА + ИИ ============
@router.message(F.text & ~F.text.startswith("/"))
async def handle_message(message: Message):
    u = message.from_user

    if PENDING_REPORTS.get(u.id):
        PENDING_REPORTS.pop(u.id, None)
        if censor(message.text) is None:
            await message.answer("⚠️ Репорт содержит запрещённые слова.")
            return

        owner_id = OWNER_ID or await get_owner_id()
        if owner_id:
            report_text = (
                "🚨 <b>НОВЫЙ РЕПОРТ</b>\n\n"
                f"👤 От: @{u.username or 'нет'}\n"
                f"🆔 ID: <code>{u.id}</code>\n"
                f"📛 Имя: {u.first_name}\n\n"
                f"💬 <b>Обращение:</b>\n{message.text}"
            )
            try:
                await bot.send_message(owner_id, report_text)
                await message.answer("✅ Твой репорт отправлен владельцу @flaybbe.")
            except Exception as e:
                logging.error(f"report send error: {e}")
                await message.answer("⚠️ Не удалось отправить репорт.")
        else:
            await message.answer("⚠️ Владелец ещё не запускал бота — репорт не доставлен.")
        return

    user = await get_user(u.id)
    if not user:
        await create_user(u.id, u.username, u.first_name)

    if censor(message.text) is None:
        await message.answer("⚠️ Сообщение содержит запрещённые слова.")
        return

    user = await get_user(u.id)
    premium = await is_premium(u.id)
    if not premium and user["messages"] >= FREE_LIMIT:
        await message.answer(
            "⚠️ <b>Лимит исчерпан</b>\n\n"
            f"Без премиума доступно только {FREE_LIMIT} сообщения.\n\n"
            "💎 Купи премиум: /premium\n"
            "✍️ Или напиши: @flaybbe"
        )
        return

    await inc_messages(u.id)

    thinking = await message.answer("⏳ Jungle AI думает...")
    answer = await ask_ai(u.id, message.text)
    try:
        await thinking.delete()
    except:
        pass

    for i in range(0, len(answer), 4000):
        await message.answer(answer[i:i+4000])

# ============ ПОИСК OWNER_ID ============
async def get_owner_id():
    global OWNER_ID
    if OWNER_ID:
        return OWNER_ID
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (OWNER_USERNAME.lower(),)) as cur:
            row = await cur.fetchone()
            if row:
                OWNER_ID = row["user_id"]
    return OWNER_ID

# ============ ЗАПУСК ============
async def main():
    await init_db()
    logging.info("Jungle AI запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())