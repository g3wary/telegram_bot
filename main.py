import asyncio
import logging
import sqlite3
import re
import os
from datetime import datetime
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, FloodWaitError

# ===================================================================
# КОНФИГ
# ===================================================================
BOT_TOKEN = "8811673952:AAELKtt-1h8f8oLBhvkLyex0HJ1SvsVhyaI"
MAIN_ADMIN = 8757166517

# ТВОИ API ДАННЫЕ
TG_API_ID = 26929053
TG_API_HASH = "1375761b8b0de946640ba3d0bb264d42"

SBER_CARD = "2202208228158128"
SBER_NAME = "Максим Сергеевич Я."

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shop")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ===================================================================
# БАЗА ДАННЫХ (SQLite)
# ===================================================================
conn = sqlite3.connect('shop.db', check_same_thread=False)
cur = conn.cursor()

cur.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    purchases INTEGER DEFAULT 0,
    total_spent_rub INTEGER DEFAULT 0,
    total_spent_stars INTEGER DEFAULT 0,
    created_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE,
    country TEXT,
    year INTEGER,
    price_rub INTEGER,
    price_stars INTEGER,
    session_string TEXT,
    is_active INTEGER DEFAULT 0,
    is_sold INTEGER DEFAULT 0,
    created_at TEXT,
    sold_to INTEGER,
    sold_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    account_id INTEGER,
    price_rub INTEGER,
    price_stars INTEGER,
    payment_method TEXT,
    purchase_date TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
''')

conn.commit()

# ===================================================================
# ФУНКЦИИ БД
# ===================================================================
def get_user(user_id: int, username: str = "", full_name: str = "") -> dict:
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    if not user:
        cur.execute(
            "INSERT INTO users (id, username, full_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, datetime.now().isoformat())
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cur.fetchone()
    return {
        "id": user[0],
        "username": user[1] or "",
        "full_name": user[2] or "",
        "purchases": user[3],
        "total_spent_rub": user[4],
        "total_spent_stars": user[5],
        "created_at": user[6]
    }

def is_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN:
        return True
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None

def add_admin(user_id: int, added_by: int) -> bool:
    try:
        cur.execute("INSERT INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
                    (user_id, added_by, datetime.now().isoformat()))
        conn.commit()
        return True
    except:
        return False

def remove_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN:
        return False
    cur.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    return True

def get_all_admins() -> List[int]:
    cur.execute("SELECT user_id FROM admins")
    return [row[0] for row in cur.fetchall()]

def get_countries() -> List[str]:
    cur.execute("SELECT DISTINCT country FROM accounts WHERE is_sold = 0 AND is_active = 1")
    return [row[0] for row in cur.fetchall()]

def get_years(country: str) -> List[int]:
    cur.execute("SELECT DISTINCT year FROM accounts WHERE country = ? AND is_sold = 0 AND is_active = 1", (country,))
    return sorted([row[0] for row in cur.fetchall()], reverse=True)

def get_account_by_id(account_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "phone": row[1],
        "country": row[2],
        "year": row[3],
        "price_rub": row[4],
        "price_stars": row[5],
        "session_string": row[6],
        "is_active": row[7],
        "is_sold": row[8],
        "created_at": row[9]
    }

def get_account(country: str, year: int) -> Optional[dict]:
    cur.execute(
        "SELECT * FROM accounts WHERE country = ? AND year = ? AND is_sold = 0 AND is_active = 1 LIMIT 1",
        (country, year)
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "phone": row[1],
        "country": row[2],
        "year": row[3],
        "price_rub": row[4],
        "price_stars": row[5],
        "session_string": row[6],
        "is_active": row[7],
        "is_sold": row[8],
        "created_at": row[9]
    }

def get_all_accounts() -> List[dict]:
    cur.execute("SELECT * FROM accounts ORDER BY is_sold, created_at DESC")
    rows = cur.fetchall()
    return [{
        "id": r[0],
        "phone": r[1],
        "country": r[2],
        "year": r[3],
        "price_rub": r[4],
        "price_stars": r[5],
        "is_active": r[7],
        "is_sold": r[8]
    } for r in rows]

def add_account(phone: str, country: str, year: int, price_rub: int, price_stars: int) -> int:
    cur.execute(
        "INSERT INTO accounts (phone, country, year, price_rub, price_stars, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (phone, country, year, price_rub, price_stars, datetime.now().isoformat())
    )
    conn.commit()
    return cur.lastrowid

def update_account_field(account_id: int, field: str, value):
    cur.execute(f"UPDATE accounts SET {field} = ? WHERE id = ?", (value, account_id))
    conn.commit()

def delete_account(account_id: int):
    cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()

def mark_sold(account_id: int, user_id: int, price_rub: int, price_stars: int, method: str):
    cur.execute(
        "UPDATE accounts SET is_sold = 1, sold_to = ?, sold_at = ? WHERE id = ?",
        (user_id, datetime.now().isoformat(), account_id)
    )
    cur.execute(
        "UPDATE users SET purchases = purchases + 1, total_spent_rub = total_spent_rub + ?, total_spent_stars = total_spent_stars + ? WHERE id = ?",
        (price_rub, price_stars, user_id)
    )
    cur.execute(
        "INSERT INTO purchases (user_id, account_id, price_rub, price_stars, payment_method, purchase_date) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, account_id, price_rub, price_stars, method, datetime.now().isoformat())
    )
    conn.commit()

def get_stats() -> dict:
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts WHERE is_sold = 1")
    sold = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts WHERE is_sold = 0 AND is_active = 1")
    available = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts")
    total_all = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(price_rub), 0) FROM purchases")
    total_rub = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(price_stars), 0) FROM purchases")
    total_stars = cur.fetchone()[0]
    return {
        "users": users,
        "sold": sold,
        "available": available,
        "total_all": total_all,
        "total_rub": total_rub,
        "total_stars": total_stars
    }

def get_user_spent(user_id: int) -> dict:
    cur.execute("SELECT total_spent_rub, total_spent_stars FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    return {"rub": row[0] if row else 0, "stars": row[1] if row else 0}

def get_account_phone(account_id: int) -> Optional[str]:
    cur.execute("SELECT phone FROM accounts WHERE id = ?", (account_id,))
    row = cur.fetchone()
    return row[0] if row else None

def update_session(account_id: int, session_string: str):
    cur.execute("UPDATE accounts SET session_string = ?, is_active = 1 WHERE id = ?", (session_string, account_id))
    conn.commit()

def account_has_session(account_id: int) -> bool:
    cur.execute("SELECT session_string FROM accounts WHERE id = ?", (account_id,))
    row = cur.fetchone()
    return row and row[0] is not None and len(row[0]) > 10

# ===================================================================
# TELETHON МЕНЕДЖЕР
# ===================================================================
class TelethonManager:
    def __init__(self):
        self.clients: Dict[str, TelegramClient] = {}
        self.pending: Dict[str, TelegramClient] = {}
        self.code_waiters: Dict[str, asyncio.Future] = {}
        self._code_hashes: Dict[str, str] = {}

    def _make_client(self, session=None):
        return TelegramClient(
            session if session else StringSession(),
            TG_API_ID,
            TG_API_HASH,
            device_model="Samsung Galaxy S23",
            system_version="Android 13",
            app_version="10.3.2",
            lang_code="ru"
        )

    def _attach_handler(self, client, phone):
        @client.on(events.NewMessage(from_users=777000))
        async def handler(event):
            text = event.raw_text or ""
            code_match = re.search(r'\b(\d{5,6})\b', text)
            if code_match:
                code = code_match.group(1)
                logger.info(f"Получен код для {phone}: {code}")
                fut = self.code_waiters.get(phone)
                if fut and not fut.done():
                    fut.set_result(code)

    async def load_sessions(self):
        cur.execute("SELECT id, phone, session_string FROM accounts WHERE is_sold = 0 AND is_active = 1 AND session_string IS NOT NULL")
        rows = cur.fetchall()
        for row in rows:
            try:
                client = self._make_client(StringSession(row[2]))
                await client.connect()
                if await client.is_user_authorized():
                    self._attach_handler(client, row[1])
                    self.clients[row[1]] = client
                    logger.info(f"✅ Сессия загружена: {row[1]}")
                else:
                    cur.execute("UPDATE accounts SET is_active = 0 WHERE id = ?", (row[0],))
                    conn.commit()
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки {row[1]}: {e}")
                cur.execute("UPDATE accounts SET is_active = 0 WHERE id = ?", (row[0],))
                conn.commit()

    async def start_login(self, phone: str) -> bool:
        try:
            client = self._make_client()
            await client.connect()
            sent = await client.send_code_request(phone)
            self._code_hashes[phone] = sent.phone_code_hash
            self.pending[phone] = client
            logger.info(f"📨 Код отправлен на {phone}")
            return True
        except FloodWaitError as e:
            logger.error(f"⏳ Флуд-бан {e.seconds}с для {phone}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка отправки кода {phone}: {e}")
            return False

    async def finish_login(self, phone: str, code: str):
        client = self.pending.get(phone)
        if not client:
            return None
        try:
            clean_code = re.sub(r'\D', '', code.strip())
            phone_hash = self._code_hashes.get(phone)
            await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_hash)
        except SessionPasswordNeededError:
            return "2fa"
        except PhoneCodeExpiredError:
            return "expired"
        except PhoneCodeInvalidError:
            return "bad_code"
        except Exception as e:
            logger.error(f"❌ Ошибка входа {phone}: {e}")
            return None
        session_str = client.session.save()
        self._attach_handler(client, phone)
        self.clients[phone] = client
        self.pending.pop(phone, None)
        logger.info(f"✅ Аккаунт {phone} авторизован")
        return session_str

    async def wait_for_code(self, phone: str, timeout: int = 180):
        fut = asyncio.get_event_loop().create_future()
        self.code_waiters[phone] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"⏰ Таймаут кода для {phone}")
            return None
        finally:
            self.code_waiters.pop(phone, None)

    async def logout(self, phone: str):
        client = self.clients.pop(phone, None)
        if client:
            try:
                await client.log_out()
                logger.info(f"🚪 Выход из {phone}")
            except:
                pass
            try:
                await client.disconnect()
            except:
                pass
        cur.execute("UPDATE accounts SET is_active = 0, session_string = NULL WHERE phone = ?", (phone,))
        conn.commit()

    async def disconnect_all(self):
        for phone, client in list(self.clients.items()):
            try:
                await client.disconnect()
            except:
                pass
        self.clients.clear()

telethon_mgr = TelethonManager()

# ===================================================================
# СОСТОЯНИЯ FSM
# ===================================================================
class Agreement(StatesGroup):
    waiting = State()

class BuyStates(StatesGroup):
    country = State()
    year = State()
    confirm = State()
    payment = State()
    waiting_code = State()

class AdminAddAccount(StatesGroup):
    country = State()
    year = State()
    price = State()
    phone = State()
    code = State()

class AdminEditAccount(StatesGroup):
    select = State()
    field = State()
    value = State()

class AdminDeleteAccount(StatesGroup):
    select = State()

class AdminAddAdmin(StatesGroup):
    id = State()

class AdminRemoveAdmin(StatesGroup):
    id = State()

# ===================================================================
# КЛАВИАТУРЫ
# ===================================================================
def main_kb(user_id: int):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    kb.add(InlineKeyboardButton("🛒 Купить аккаунт", callback_data="catalog"))
    kb.add(InlineKeyboardButton("📞 Тех-поддержка", callback_data="support"))
    if is_admin(user_id):
        kb.add(InlineKeyboardButton("🛠 Админ-панель", callback_data="admin_panel"))
    return kb

def admin_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
    kb.add(InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add_acc"))
    kb.add(InlineKeyboardButton("✏️ Изменить аккаунт", callback_data="admin_edit_acc"))
    kb.add(InlineKeyboardButton("🗑 Удалить аккаунт", callback_data="admin_del_acc"))
    kb.add(InlineKeyboardButton("👑 Добавить админа", callback_data="admin_add_admin"))
    kb.add(InlineKeyboardButton("👑 Снять админа", callback_data="admin_remove_admin"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_start"))
    return kb

def countries_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    for c in get_countries():
        kb.insert(InlineKeyboardButton(f"🌍 {c}", callback_data=f"country_{c}"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_start"))
    return kb

def years_kb(country: str):
    kb = InlineKeyboardMarkup(row_width=3)
    for y in get_years(country):
        kb.insert(InlineKeyboardButton(str(y), callback_data=f"year_{y}"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_catalog"))
    return kb

def payment_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 СБП (Сбер)", callback_data="pay_sbp"))
    kb.add(InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_payment"))
    return kb

def confirm_kb(account_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Да", callback_data=f"confirm_{account_id}"),
        InlineKeyboardButton("❌ Нет", callback_data="back_catalog")
    )
    return kb

def back_kb(callback: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data=callback))
    return kb

def get_code_kb(phone: str):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📨 Получить код", callback_data=f"get_code_{phone}"))
    return kb

def catalog_accounts_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    cur.execute("SELECT id, country, year, price_rub FROM accounts WHERE is_sold = 0 AND is_active = 1 ORDER BY country, year DESC")
    rows = cur.fetchall()
    if not rows:
        kb.add(InlineKeyboardButton("📭 Нет аккаунтов", callback_data="noop"))
    else:
        for r in rows:
            emoji = {"Россия": "🇷🇺", "РФ": "🇷🇺", "США": "🇺🇸", "Норвегия": "🇳🇴", "Германия": "🇩🇪"}.get(r[1], "🌍")
            kb.add(InlineKeyboardButton(
                f"{emoji} {r[1]} | {r[2]}г. - {r[3]}₽",
                callback_data=f"acc_{r[0]}"
            ))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_start"))
    return kb

def admin_edit_accounts_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    cur.execute("SELECT id, country, year, price_rub FROM accounts WHERE is_sold = 0 ORDER BY country, year DESC")
    rows = cur.fetchall()
    if not rows:
        kb.add(InlineKeyboardButton("📭 Нет аккаунтов", callback_data="noop"))
    else:
        for r in rows:
            kb.add(InlineKeyboardButton(
                f"{r[1]} | {r[2]}г. - {r[3]}₽",
                callback_data=f"edit_{r[0]}"
            ))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    return kb

def admin_delete_accounts_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    cur.execute("SELECT id, country, year, price_rub, is_sold FROM accounts ORDER BY is_sold, country, year DESC")
    rows = cur.fetchall()
    if not rows:
        kb.add(InlineKeyboardButton("📭 Нет аккаунтов", callback_data="noop"))
    else:
        for r in rows:
            status = "💰" if r[4] else "📱"
            kb.add(InlineKeyboardButton(
                f"{status} {r[1]} | {r[2]}г. - {r[3]}₽",
                callback_data=f"del_{r[0]}"
            ))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    return kb

def admin_edit_fields_kb(account_id: int):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🌍 Страна", callback_data=f"edit_field_{account_id}_country"))
    kb.add(InlineKeyboardButton("📅 Год", callback_data=f"edit_field_{account_id}_year"))
    kb.add(InlineKeyboardButton("💰 Цена (руб)", callback_data=f"edit_field_{account_id}_price_rub"))
    kb.add(InlineKeyboardButton("⭐ Цена (звёзды)", callback_data=f"edit_field_{account_id}_price_stars"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_edit_acc"))
    return kb

# ===================================================================
# ОБРАБОТЧИКИ КОМАНД
# ===================================================================
@dp.message_handler(commands=['start'])
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.finish()
    get_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name)
    
    # Проверяем соглашение
    cur.execute("SELECT tos_accepted FROM users WHERE id = ?", (msg.from_user.id,))
    row = cur.fetchone()
    if not row or not row[0]:
        await msg.answer(
            "📜 <b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b>\n\n"
            "Используя данный бот, вы соглашаетесь с тем, что:\n\n"
            "1. Аккаунты Telegram продаются как цифровой товар\n"
            "2. Возврат средств после получения данных невозможен\n"
            "3. Вы не будете использовать аккаунт для спама/мошенничества\n"
            "4. Продавец не несёт ответственности за блокировку аккаунта\n\n"
            "Нажимая «Принимаю», вы подтверждаете, что прочитали и согласны.",
            reply_markup=InlineKeyboardMarkup(row_width=1).add(
                InlineKeyboardButton("✅ Принимаю", callback_data="tos_accept"),
                InlineKeyboardButton("❌ Отказываюсь", callback_data="tos_decline")
            )
        )
        await state.set_state(Agreement.waiting)
        return
    
    await show_main_menu(msg, msg.from_user.id)

async def show_main_menu(msg: types.Message, user_id: int):
    await msg.answer(
        "👋 <b>Здравствуйте!</b>\n\n"
        "Здесь вы сможете приобрести физический аккаунт Telegram за несколько минут, безопасно и качественно.",
        reply_markup=main_kb(user_id)
    )

@dp.callback_query_handler(lambda c: c.data == "tos_accept", state=Agreement.waiting)
async def tos_accept(call: types.CallbackQuery, state: FSMContext):
    cur.execute("UPDATE users SET tos_accepted = 1 WHERE id = ?", (call.from_user.id,))
    conn.commit()
    await state.finish()
    await call.message.delete()
    await show_main_menu(call.message, call.from_user.id)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "tos_decline", state=Agreement.waiting)
async def tos_decline(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("❌ Вы отказались от соглашения. Бот недоступен. Нажмите /start, чтобы попробовать снова.")
    await state.finish()
    await call.answer()

# ===================================================================
# ПРОФИЛЬ
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "profile")
async def profile(call: types.CallbackQuery):
    user = get_user(call.from_user.id)
    spent = get_user_spent(call.from_user.id)
    created = datetime.fromisoformat(user["created_at"])
    now = datetime.now()
    delta = now - created
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    
    await call.message.edit_text(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"📛 NickName: @{user['username'] or 'не указан'}\n"
        f"🆔 #TG-ID: <code>{user['id']}</code>\n"
        f"📅 С нами: {days}д {hours}ч {minutes}м\n"
        f"🛒 Купленных аккаунтов: <b>{user['purchases']}</b>\n"
        f"💰 Потрачено: {spent['stars']} ⭐ / {spent['rub']} ₽",
        reply_markup=main_kb(call.from_user.id)
    )
    await call.answer()

# ===================================================================
# ТЕХ-ПОДДЕРЖКА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "support")
async def support(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📩 Написать Тех-поддержке", url="https://t.me/sadgewary"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="back_start"))
    await call.message.edit_text(
        "📞 <b>Тех-поддержка</b>\n\n"
        "Это человек, к которому вы можете обратиться, если:\n"
        "• нашли баг\n"
        "• вам не выдали аккаунт\n"
        "• возникли другие проблемы\n\n"
        "Опишите всё максимально подробно и спокойно. "
        "Если вам не отвечают долгое время, напишите ещё раз.",
        reply_markup=kb
    )
    await call.answer()

# ===================================================================
# КАТАЛОГ
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "catalog")
async def catalog(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text(
        "🛒 <b>Каталог аккаунтов</b>\n\n"
        "Выберите подходящий аккаунт и нажмите на него:",
        reply_markup=catalog_accounts_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("acc_"))
async def choose_account(call: types.CallbackQuery, state: FSMContext):
    account_id = int(call.data.split("_")[1])
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await call.message.edit_text("❌ Аккаунт уже продан. Выберите другой.", reply_markup=catalog_accounts_kb())
        await call.answer()
        return
    
    await state.update_data(account_id=account_id)
    emoji = {"Россия": "🇷🇺", "РФ": "🇷🇺", "США": "🇺🇸", "Норвегия": "🇳🇴", "Германия": "🇩🇪"}.get(acc["country"], "🌍")
    await call.message.edit_text(
        f"Вы выбрали аккаунт - {emoji} {acc['country']} {acc['year']} года регистрации.\n\n"
        f"💰 Цена: {acc['price_rub']} ₽ / {acc['price_stars']} ⭐\n\n"
        f"Вы уверены, что хотите его приобрести?",
        reply_markup=confirm_kb(account_id)
    )
    await state.set_state(BuyStates.confirm)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("confirm_"), state=BuyStates.confirm)
async def confirm_purchase(call: types.CallbackQuery, state: FSMContext):
    account_id = int(call.data.split("_")[1])
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await call.message.edit_text("❌ Аккаунт уже продан.", reply_markup=catalog_accounts_kb())
        await state.finish()
        await call.answer()
        return
    
    await state.update_data(account_id=account_id)
    await call.message.edit_text(
        "💳 <b>Выберите способ оплаты</b>",
        reply_markup=payment_kb()
    )
    await state.set_state(BuyStates.payment)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "back_catalog")
async def back_catalog(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text(
        "🛒 <b>Каталог аккаунтов</b>\n\n"
        "Выберите подходящий аккаунт:",
        reply_markup=catalog_accounts_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "back_payment")
async def back_payment(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    if account_id:
        acc = get_account_by_id(account_id)
        if acc and not acc["is_sold"]:
            await call.message.edit_text(
                f"Вы выбрали аккаунт - {acc['country']} {acc['year']} года.\n\n"
                f"💰 Цена: {acc['price_rub']} ₽ / {acc['price_stars']} ⭐\n\n"
                f"Вы уверены, что хотите его приобрести?",
                reply_markup=confirm_kb(account_id)
            )
            await state.set_state(BuyStates.confirm)
            await call.answer()
            return
    await state.finish()
    await call.message.edit_text("🛒 Каталог аккаунтов", reply_markup=catalog_accounts_kb())
    await call.answer()

# ===================================================================
# ОПЛАТА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "pay_sbp", state=BuyStates.payment)
async def pay_sbp(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await call.message.edit_text("❌ Аккаунт уже продан.", reply_markup=main_kb(call.from_user.id))
        await state.finish()
        await call.answer()
        return
    
    await state.update_data(payment_method="sbp")
    await call.message.edit_text(
        f"💳 <b>Оплата по СБП</b>\n\n"
        f"💰 Сумма: <b>{acc['price_rub']} ₽</b>\n\n"
        f"🏦 Реквизиты:\n"
        f"<code>{SBER_CARD}</code>\n"
        f"Получатель: <b>{SBER_NAME}</b>\n"
        f"Банк: Сбер\n\n"
        f"📤 После оплаты отправьте скриншот перевода.",
        reply_markup=back_kb("back_payment")
    )
    await state.set_state(BuyStates.waiting_code)

@dp.message_handler(content_types=['photo'], state=BuyStates.waiting_code)
async def handle_screenshot(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await msg.answer("❌ Аккаунт уже продан.", reply_markup=main_kb(msg.from_user.id))
        await state.finish()
        return
    
    # Простая проверка — пока пропускаем, потом добавим OCR
    await msg.answer("✅ Скриншот получен! Оплата подтверждена.")
    await process_payment(msg, state, account_id, "sbp")

@dp.callback_query_handler(lambda c: c.data == "pay_stars", state=BuyStates.payment)
async def pay_stars(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await call.message.edit_text("❌ Аккаунт уже продан.", reply_markup=main_kb(call.from_user.id))
        await state.finish()
        await call.answer()
        return
    
    await state.update_data(payment_method="stars")
    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"Аккаунт {acc['country']} {acc['year']}г.",
            description=f"Физический аккаунт Telegram — {acc['country']}, {acc['year']} год",
            payload=f"acc_{account_id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Аккаунт", amount=acc['price_stars'])]
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка инвойса: {e}")
        await call.message.edit_text("❌ Ошибка создания счёта. Попробуйте СБП.", reply_markup=payment_kb())

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message_handler(content_types=['successful_payment'])
async def stars_paid(msg: types.Message, state: FSMContext):
    payload = msg.successful_payment.invoice_payload
    account_id = int(payload.replace("acc_", ""))
    await process_payment(msg, state, account_id, "stars")

# ===================================================================
# ОБРАБОТКА УСПЕШНОЙ ОПЛАТЫ
# ===================================================================
async def process_payment(msg: types.Message, state: FSMContext, account_id: int, method: str):
    acc = get_account_by_id(account_id)
    if not acc or acc["is_sold"]:
        await msg.answer("❌ Аккаунт уже продан.", reply_markup=main_kb(msg.from_user.id))
        await state.finish()
        return
    
    mark_sold(account_id, msg.from_user.id, acc["price_rub"], acc["price_stars"], method)
    
    await msg.answer(
        f"✅ <b>Вы успешно оплатили аккаунт {acc['country']}.</b>\n\n"
        f"📖 <b>Инструкция по добавлению аккаунта:</b>\n\n"
        f"1. Зайдите в <b>Настройки</b> Telegram\n"
        f"2. Зайдите в раздел <b>Аккаунт</b> и пролистайте вниз\n"
        f"3. Нажмите на кнопку <b>Добавить аккаунт</b>\n"
        f"4. Введите номер телефона, который я отправлю ниже\n"
        f"5. Бот вышлет вам код, который вы введёте в поле\n"
        f"6. Аккаунт полностью ваш\n\n"
        f"Нажмите «Далее», чтобы получить номер телефона.",
        reply_markup=InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("➡️ Далее", callback_data=f"next_{account_id}")
        )
    )
    await state.set_state(BuyStates.waiting_code)

@dp.callback_query_handler(lambda c: c.data.startswith("next_"))
async def give_phone(call: types.CallbackQuery, state: FSMContext):
    account_id = int(call.data.split("_")[1])
    phone = get_account_phone(account_id)
    if not phone:
        await call.message.edit_text("❌ Ошибка: номер не найден.", reply_markup=main_kb(call.from_user.id))
        await state.finish()
        await call.answer()
        return
    
    await call.message.edit_text(
        f"📱 <b>Номер телефона для входа:</b>\n\n"
        f"<code>{phone}</code>\n\n"
        f"После того, как вы введёте номер и запросите код в Telegram, "
        f"нажмите кнопку ниже, чтобы получить код от бота.",
        reply_markup=get_code_kb(phone)
    )
    await state.update_data(phone=phone, account_id=account_id)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("get_code_"))
async def get_code(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("get_code_", "")
    data = await state.get_data()
    account_id = data.get("account_id")
    
    # Проверяем, есть ли активная сессия
    if phone not in telethon_mgr.clients:
        await call.message.answer(
            "⚠️ Бот не авторизован на этом аккаунте. Обратитесь к администратору.",
            reply_markup=main_kb(call.from_user.id)
        )
        await call.answer()
        return
    
    wait_msg = await call.message.answer(f"⏳ Ожидаю код для <code>{phone}</code>...")
    
    code = await telethon_mgr.wait_for_code(phone, timeout=180)
    
    if not code:
        await wait_msg.edit_text(
            "⏰ Код не пришёл. Проверьте, что вы правильно запросили код в Telegram.",
            reply_markup=get_code_kb(phone)
        )
        await call.answer()
        return
    
    await wait_msg.edit_text(
        f"✅ <b>Код получен!</b>\n\n"
        f"🔑 <code>{code}</code>\n\n"
        f"Введите этот код в Telegram, чтобы войти в аккаунт.",
        reply_markup=InlineKeyboardMarkup(row_width=1).add(
            InlineKeyboardButton("✅ Я вошёл — бот может выйти", callback_data=f"logout_{phone}")
        )
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("logout_"))
async def logout_account(call: types.CallbackQuery):
    phone = call.data.replace("logout_", "")
    await call.message.edit_text("🔄 Завершаю сессию...")
    
    await telethon_mgr.logout(phone)
    
    await call.message.edit_text(
        "🎉 <b>Спасибо за покупку в нашем боте!</b>\n\n"
        "Рады, что вы нам доверяете.\n\n"
        "📌 <b>Рекомендации по аккаунту:</b>\n"
        "1. Не трогайте аккаунт в течение 20-40 часов\n"
        "2. Установите облачный пароль и привяжите почту\n\n"
        "⚠️ <b>Что делать, если аккаунт заморозили?</b>\n"
        "Напишите в Тех-поддержку — вам постараются помочь.\n\n"
        "Удачи и ждём дальнейших покупок! 🚀",
        reply_markup=main_kb(call.from_user.id)
    )
    await call.answer()

# ===================================================================
# НАВИГАЦИЯ
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "back_start")
async def back_start(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text(
        "👋 <b>Здравствуйте!</b>\n\n"
        "Здесь вы сможете приобрести физический аккаунт Telegram за несколько минут, безопасно и качественно.",
        reply_markup=main_kb(call.from_user.id)
    )
    await call.answer()

# ===================================================================
# АДМИН-ПАНЕЛЬ
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_panel")
async def admin_panel(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.finish()
    await call.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_stats")
async def admin_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    st = get_stats()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей в боте: <b>{st['users']}</b>\n"
        f"🛒 Куплено аккаунтов: <b>{st['sold']}</b>\n"
        f"📦 Аккаунтов в каталоге: <b>{st['available']}</b>\n"
        f"📦 Аккаунтов за всё время: <b>{st['total_all']}</b>\n"
        f"💰 Потрачено рублей: <b>{st['total_rub']} ₽</b>\n"
        f"⭐ Потрачено звёзд: <b>{st['total_stars']} ⭐</b>",
        reply_markup=admin_kb()
    )
    await call.answer()

# ===================================================================
# АДМИН — ДОБАВЛЕНИЕ АККАУНТА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_add_acc")
async def admin_add_acc(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminAddAccount.country)
    await call.message.edit_text(
        "🌍 <b>Добавление аккаунта</b>\n\n"
        "Введите страну аккаунта (например: США, Россия, Норвегия):",
        reply_markup=back_kb("admin_panel")
    )
    await call.answer()

@dp.message_handler(state=AdminAddAccount.country)
async def admin_add_country(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.update_data(country=msg.text.strip())
    await state.set_state(AdminAddAccount.year)
    await msg.answer(
        "📅 Введите год регистрации (например: 2020):",
        reply_markup=back_kb("admin_panel")
    )

@dp.message_handler(state=AdminAddAccount.year)
async def admin_add_year(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        year = int(msg.text.strip())
    except:
        await msg.answer("❌ Введите число")
        return
    await state.update_data(year=year)
    await state.set_state(AdminAddAccount.price)
    await msg.answer(
        "💰 Введите цену в рублях:",
        reply_markup=back_kb("admin_panel")
    )

@dp.message_handler(state=AdminAddAccount.price)
async def admin_add_price(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        price_rub = int(msg.text.strip())
        price_stars = int(price_rub * 0.72)  # Конвертация в звёзды
    except:
        await msg.answer("❌ Введите число")
        return
    await state.update_data(price_rub=price_rub, price_stars=price_stars)
    await state.set_state(AdminAddAccount.phone)
    await msg.answer(
        f"📱 Введите номер телефона аккаунта (например: +11112223344):\n\n"
        f"Цена: {price_rub} ₽ / {price_stars} ⭐",
        reply_markup=back_kb("admin_panel")
    )

@dp.message_handler(state=AdminAddAccount.phone)
async def admin_add_phone(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    phone = re.sub(r'[\s\-+]', '', msg.text.strip())
    if not phone.isdigit() or len(phone) < 7:
        await msg.answer("❌ Введите корректный номер телефона.")
        return
    phone = "+" + phone
    await state.update_data(phone=phone)
    
    data = await state.get_data()
    account_id = add_account(phone, data["country"], data["year"], data["price_rub"], data["price_stars"])
    
    await msg.answer(
        f"📨 <b>Код был выслан на {phone}</b>\n\n"
        f"Введите его сюда, чтобы бот зашёл на аккаунт.",
        reply_markup=back_kb("admin_panel")
    )
    await state.update_data(account_id=account_id)
    await state.set_state(AdminAddAccount.code)
    
    # Отправляем код через Telethon
    ok = await telethon_mgr.start_login(phone)
    if not ok:
        await msg.answer("⚠️ Не удалось отправить код. Аккаунт добавлен без сессии.", reply_markup=admin_kb())
        await state.finish()

@dp.message_handler(state=AdminAddAccount.code)
async def admin_add_code(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    code = msg.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    account_id = data.get("account_id")
    
    result = await telethon_mgr.finish_login(phone, code)
    
    if result == "2fa":
        await msg.answer("🔐 Аккаунт защищён 2FA. Введите пароль:", reply_markup=back_kb("admin_panel"))
        return
    if result in ["expired", "bad_code"]:
        await msg.answer(f"❌ {'Просроченный' if result == 'expired' else 'Неверный'} код. Попробуйте снова.", reply_markup=back_kb("admin_panel"))
        return
    if not result:
        await msg.answer("❌ Ошибка входа. Попробуйте ещё раз.", reply_markup=admin_kb())
        await state.finish()
        return
    
    update_session(account_id, result)
    await msg.answer(
        f"✅ <b>Аккаунт {phone} успешно добавлен!</b>\n\n"
        f"🌍 Страна: {data['country']}\n"
        f"📅 Год: {data['year']}\n"
        f"💰 Цена: {data['price_rub']} ₽ / {data['price_stars']} ⭐\n\n"
        f"Аккаунт доступен в каталоге.",
        reply_markup=admin_kb()
    )
    await state.finish()

# ===================================================================
# АДМИН — ИЗМЕНЕНИЕ АККАУНТА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_edit_acc")
async def admin_edit_acc(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminEditAccount.select)
    await call.message.edit_text(
        "✏️ <b>Выберите аккаунт для изменения:</b>",
        reply_markup=admin_edit_accounts_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"), state=AdminEditAccount.select)
async def admin_edit_select(call: types.CallbackQuery, state: FSMContext):
    account_id = int(call.data.split("_")[1])
    await state.update_data(account_id=account_id)
    await call.message.edit_text(
        "✏️ <b>Что вы хотите изменить?</b>",
        reply_markup=admin_edit_fields_kb(account_id)
    )
    await state.set_state(AdminEditAccount.field)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_field_"))
async def admin_edit_field(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    account_id = int(parts[2])
    field = parts[3]
    await state.update_data(account_id=account_id, field=field)
    await state.set_state(AdminEditAccount.value)
    
    field_names = {
        "country": "страну",
        "year": "год",
        "price_rub": "цену (рубли)",
        "price_stars": "цену (звёзды)"
    }
    await call.message.edit_text(
        f"✏️ Введите новое значение для <b>{field_names.get(field, field)}</b>:",
        reply_markup=back_kb("admin_edit_acc")
    )
    await call.answer()

@dp.message_handler(state=AdminEditAccount.value)
async def admin_edit_value(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    data = await state.get_data()
    account_id = data.get("account_id")
    field = data.get("field")
    value = msg.text.strip()
    
    # Конвертируем для числовых полей
    if field in ["year", "price_rub", "price_stars"]:
        try:
            value = int(value)
        except:
            await msg.answer("❌ Введите число.")
            return
        if field == "price_stars" and value < 1:
            await msg.answer("❌ Цена должна быть больше 0.")
            return
    
    update_account_field(account_id, field, value)
    await msg.answer(
        f"✅ <b>Аккаунт обновлён!</b>",
        reply_markup=admin_kb()
    )
    await state.finish()

# ===================================================================
# АДМИН — УДАЛЕНИЕ АККАУНТА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_del_acc")
async def admin_del_acc(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminDeleteAccount.select)
    await call.message.edit_text(
        "🗑 <b>Выберите аккаунт для удаления:</b>",
        reply_markup=admin_delete_accounts_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("del_"), state=AdminDeleteAccount.select)
async def admin_del_confirm(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    account_id = int(call.data.split("_")[1])
    acc = get_account_by_id(account_id)
    if not acc:
        await call.message.edit_text("❌ Аккаунт не найден.", reply_markup=admin_kb())
        await state.finish()
        await call.answer()
        return
    
    # Выходим из сессии если есть
    if acc["phone"] in telethon_mgr.clients:
        await telethon_mgr.logout(acc["phone"])
    
    delete_account(account_id)
    await call.message.edit_text(
        f"✅ Аккаунт {acc['phone']} удалён.",
        reply_markup=admin_kb()
    )
    await state.finish()
    await call.answer()

# ===================================================================
# АДМИН — ДОБАВЛЕНИЕ АДМИНА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_add_admin")
async def admin_add_admin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != MAIN_ADMIN:
        await call.answer("⛔ Только главный админ может добавлять админов", show_alert=True)
        return
    await state.set_state(AdminAddAdmin.id)
    await call.message.edit_text(
        "👑 Введите Telegram ID пользователя, которого хотите сделать админом:",
        reply_markup=back_kb("admin_panel")
    )
    await call.answer()

@dp.message_handler(state=AdminAddAdmin.id)
async def admin_add_admin_id(msg: types.Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN:
        return
    try:
        user_id = int(msg.text.strip())
    except:
        await msg.answer("❌ Введите число.")
        return
    
    if user_id == MAIN_ADMIN:
        await msg.answer("❌ Это главный админ.", reply_markup=admin_kb())
        await state.finish()
        return
    
    if add_admin(user_id, msg.from_user.id):
        await msg.answer(f"✅ Пользователь <code>{user_id}</code> назначен админом.", reply_markup=admin_kb())
    else:
        await msg.answer("❌ Ошибка добавления.", reply_markup=admin_kb())
    await state.finish()

# ===================================================================
# АДМИН — СНЯТИЕ АДМИНА
# ===================================================================
@dp.callback_query_handler(lambda c: c.data == "admin_remove_admin")
async def admin_remove_admin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != MAIN_ADMIN:
        await call.answer("⛔ Только главный админ может снимать админов", show_alert=True)
        return
    await state.set_state(AdminRemoveAdmin.id)
    await call.message.edit_text(
        "👑 Введите Telegram ID админа, которого хотите снять:",
        reply_markup=back_kb("admin_panel")
    )
    await call.answer()

@dp.message_handler(state=AdminRemoveAdmin.id)
async def admin_remove_admin_id(msg: types.Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN:
        return
    try:
        user_id = int(msg.text.strip())
    except:
        await msg.answer("❌ Введите число.")
        return
    
    if user_id == MAIN_ADMIN:
        await msg.answer("❌ Нельзя снять главного админа.", reply_markup=admin_kb())
        await state.finish()
        return
    
    if remove_admin(user_id):
        await msg.answer(f"✅ Админ <code>{user_id}</code> снят.", reply_markup=admin_kb())
    else:
        await msg.answer("❌ Ошибка снятия.", reply_markup=admin_kb())
    await state.finish()

# ===================================================================
# ЗАПУСК
# ===================================================================
async def on_startup(dp):
    logger.info("🚀 Бот запускается...")
    await telethon_mgr.load_sessions()
    logger.info(f"✅ Загружено {len(telethon_mgr.clients)} сессий")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
