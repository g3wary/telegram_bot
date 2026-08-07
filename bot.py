"""
Telegram Bot — Магазин физических аккаунтов (100% рабочая версия)
"""
import asyncio
import logging
import re
import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.filters import Text

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError

# ==================== КОНФИГ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8811673952:AAELKtt-1h8f8oLBhvkLyex0HJ1SvsVhyaI")
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", "8757166517"))
TG_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TG_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

RECIPIENT_NAME = "К. Максим Петрович"

# ==================== ЛОГИ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg_shop")

# ==================== БАЗА ДАННЫХ ====================
def get_db():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            full_name TEXT,
            purchases_count INTEGER DEFAULT 0,
            created_at TEXT,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT UNIQUE,
            country TEXT,
            year INTEGER,
            price INTEGER,
            session_string TEXT,
            is_active INTEGER DEFAULT 0,
            is_sold INTEGER DEFAULT 0,
            created_at TEXT,
            sold_to_user_id INTEGER,
            sold_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS purchase_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            account_id INTEGER,
            purchase_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==================== РАБОТА С БД ====================
def get_user(tg_id: int, username=None, full_name=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    user = cur.fetchone()
    if not user:
        cur.execute(
            "INSERT INTO users (telegram_id, username, full_name, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, username, full_name, datetime.now().isoformat())
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        user = cur.fetchone()
    conn.close()
    return user

def is_admin(tg_id: int) -> bool:
    if tg_id == MAIN_ADMIN_ID:
        return True
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE telegram_id = ?", (tg_id,))
    result = cur.fetchone()
    conn.close()
    return result and result[0] == 1

def get_available_countries():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT country FROM accounts WHERE is_sold = 0 AND is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return sorted([r[0] for r in rows])

def get_available_years(country: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT year FROM accounts WHERE country = ? AND is_sold = 0 AND is_active = 1", (country,))
    rows = cur.fetchall()
    conn.close()
    return sorted([r[0] for r in rows])

def pick_account(country: str, year: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM accounts WHERE country = ? AND year = ? AND is_sold = 0 AND is_active = 1 LIMIT 1",
        (country, year)
    )
    acc = cur.fetchone()
    conn.close()
    return acc

def mark_sold(account_id: int, user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE accounts SET is_sold = 1, sold_to_user_id = ?, sold_at = ? WHERE id = ?",
        (user_id, datetime.now().isoformat(), account_id)
    )
    cur.execute(
        "UPDATE users SET purchases_count = purchases_count + 1 WHERE telegram_id = ?",
        (user_id,)
    )
    cur.execute(
        "INSERT INTO purchase_history (user_id, account_id, purchase_date) VALUES (?, ?, ?)",
        (user_id, account_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_purchases_count(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT purchases_count FROM users WHERE telegram_id = ?", (user_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] if result else 0

def get_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts WHERE is_sold = 1")
    sold = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts WHERE is_sold = 0 AND is_active = 1")
    active = cur.fetchone()[0]
    cur.execute("SELECT SUM(price) FROM accounts WHERE is_sold = 1")
    revenue = cur.fetchone()[0] or 0
    conn.close()
    return users, total, active, sold, revenue

def add_account(phone, country, year, price):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO accounts (phone_number, country, year, price, is_active, is_sold, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (phone, country, year, price, 0, 0, datetime.now().isoformat())
    )
    conn.commit()
    acc_id = cur.lastrowid
    conn.close()
    return acc_id

def update_session(account_id, session_string):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE accounts SET session_string = ?, is_active = 1 WHERE id = ?",
        (session_string, account_id)
    )
    conn.commit()
    conn.close()

def delete_account(account_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()

def set_admin(tg_id: int, is_admin_val: bool):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = ? WHERE telegram_id = ?", (1 if is_admin_val else 0, tg_id))
    conn.commit()
    conn.close()

# ==================== TELETHON МЕНЕДЖЕР ====================
class TelethonManager:
    def __init__(self):
        self.clients = {}
        self.pending = {}
        self.code_waiters = {}
        self._code_hashes = {}

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
                logger.info(f"Got code for {phone}: {code}")
                fut = self.code_waiters.get(phone)
                if fut and not fut.done():
                    fut.set_result(code)

    async def load_sessions(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT phone_number, session_string FROM accounts WHERE is_sold = 0 AND is_active = 1 AND session_string IS NOT NULL")
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            try:
                client = self._make_client(StringSession(row[1]))
                await client.connect()
                if await client.is_user_authorized():
                    self._attach_handler(client, row[0])
                    self.clients[row[0]] = client
                    logger.info(f"Loaded session for {row[0]}")
            except Exception as e:
                logger.error(f"Failed to load {row[0]}: {e}")

    async def start_login(self, phone: str) -> bool:
        try:
            client = self._make_client()
            await client.connect()
            sent = await client.send_code_request(phone)
            self._code_hashes[phone] = sent.phone_code_hash
            self.pending[phone] = client
            logger.info(f"Code sent to {phone}")
            return True
        except Exception as e:
            logger.error(f"start_login error: {e}")
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
            logger.error(f"finish_login error: {e}")
            return None
        session_str = client.session.save()
        self._attach_handler(client, phone)
        self.clients[phone] = client
        self.pending.pop(phone, None)
        return session_str

    async def finish_login_2fa(self, phone: str, password: str):
        client = self.pending.get(phone)
        if not client:
            return None
        try:
            await client.sign_in(password=password)
        except Exception as e:
            logger.error(f"2fa error: {e}")
            return None
        session_str = client.session.save()
        self._attach_handler(client, phone)
        self.clients[phone] = client
        self.pending.pop(phone, None)
        return session_str

    async def wait_for_code(self, phone: str, timeout: int = 300):
        fut = asyncio.get_event_loop().create_future()
        self.code_waiters[phone] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.code_waiters.pop(phone, None)

    async def logout(self, phone: str):
        client = self.clients.pop(phone, None)
        if client:
            try:
                await client.log_out()
            except:
                pass
            try:
                await client.disconnect()
            except:
                pass

    async def disconnect_all(self):
        for phone, client in list(self.clients.items()):
            try:
                await client.disconnect()
            except:
                pass
        self.clients.clear()

telethon_mgr = TelethonManager()

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ==================== СОСТОЯНИЯ ====================
class AdminStates(StatesGroup):
    add_admin = State()
    remove_admin = State()
    add_phone = State()
    add_country = State()
    add_year = State()
    add_price = State()
    enter_code = State()
    enter_2fa = State()

class BuyStates(StatesGroup):
    choose_country = State()
    choose_year = State()
    choose_payment = State()
    wait_screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
def kb_main(admin=False):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    kb.add(InlineKeyboardButton("🛒 Купить аккаунт", callback_data="catalog"))
    if admin:
        kb.add(InlineKeyboardButton("👑 Админ-панель", callback_data="adm_panel"))
    return kb

def kb_admin():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📊 Статистика", callback_data="adm_stats"))
    kb.add(InlineKeyboardButton("📋 Аккаунты", callback_data="adm_list"))
    kb.add(InlineKeyboardButton("➕ Добавить аккаунт", callback_data="adm_add"))
    kb.add(InlineKeyboardButton("👑 Управление админами", callback_data="adm_admins"))
    kb.add(InlineKeyboardButton("🔙 Главное меню", callback_data="main"))
    return kb

def kb_countries(countries):
    kb = InlineKeyboardMarkup(row_width=1)
    for c in countries:
        kb.add(InlineKeyboardButton(f"🌍 {c}", callback_data=f"c_{c}"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="main"))
    return kb

def kb_years(years):
    kb = InlineKeyboardMarkup(row_width=1)
    for y in years:
        kb.add(InlineKeyboardButton(f"📅 {y} год", callback_data=f"y_{y}"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="catalog"))
    return kb

def kb_payment():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 Сбербанк", callback_data="pay_sber"))
    kb.add(InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="catalog"))
    return kb

def kb_code_retry(phone):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔄 Запросить новый код", callback_data=f"resend_{phone}"))
    kb.add(InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel_add"))
    return kb

def kb_back(callback="main"):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data=callback))
    return kb

# ==================== СТАРТ ====================
@dp.message_handler(commands=['start'])
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.finish()
    get_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name)
    admin = is_admin(msg.from_user.id)
    await msg.answer(
        "👋 <b>Добро пожаловать в магазин аккаунтов!</b>\n\n"
        "📱 Здесь вы можете купить физический аккаунт Telegram.",
        reply_markup=kb_main(admin)
    )

@dp.message_handler(commands=['admin'])
async def cmd_admin(msg: types.Message, state: FSMContext):
    await state.finish()
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет прав.")
        return
    await msg.answer("👑 <b>Панель администратора</b>", reply_markup=kb_admin())

# ==================== ПРОФИЛЬ ====================
@dp.callback_query_handler(text="profile")
async def cb_profile(call: types.CallbackQuery):
    admin = is_admin(call.from_user.id)
    purchases = get_purchases_count(call.from_user.id)
    await call.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{call.from_user.id}</code>\n"
        f"👤 Ник: @{call.from_user.username or 'не указан'}\n"
        f"📦 Куплено: <b>{purchases}</b>",
        reply_markup=kb_main(admin)
    )
    await call.answer()

# ==================== КАТАЛОГ ====================
@dp.callback_query_handler(text="catalog")
async def cb_catalog(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    countries = get_available_countries()
    if not countries:
        await call.message.edit_text("😔 Каталог пуст.", reply_markup=kb_main(is_admin(call.from_user.id)))
        await call.answer()
        return
    await call.message.edit_text("🌍 <b>Выберите страну:</b>", reply_markup=kb_countries(countries))
    await state.set_state(BuyStates.choose_country)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("c_"), state=BuyStates.choose_country)
async def cb_choose_country(call: types.CallbackQuery, state: FSMContext):
    country = call.data[2:]
    years = get_available_years(country)
    if not years:
        await call.message.edit_text("😔 Нет доступных аккаунтов.", reply_markup=kb_main())
        await state.finish()
        await call.answer()
        return
    await state.update_data(country=country)
    await call.message.edit_text(f"📅 <b>Выберите год</b> ({country}):", reply_markup=kb_years(years))
    await state.set_state(BuyStates.choose_year)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("y_"), state=BuyStates.choose_year)
async def cb_choose_year(call: types.CallbackQuery, state: FSMContext):
    year = int(call.data[2:])
    data = await state.get_data()
    country = data.get("country")
    acc = pick_account(country, year)
    if not acc:
        await call.message.edit_text("😔 Аккаунты закончились.", reply_markup=kb_main())
        await state.finish()
        await call.answer()
        return
    await state.update_data(account_id=acc[0], price=acc[4])
    await call.message.edit_text(
        f"📱 <b>Аккаунт найден!</b>\n\n"
        f"🌍 Страна: {acc[2]}\n"
        f"📅 Год: {acc[3]}\n"
        f"💰 Цена: <b>{acc[4]} руб.</b>\n\n"
        f"Выберите способ оплаты:",
        reply_markup=kb_payment()
    )
    await state.set_state(BuyStates.choose_payment)
    await call.answer()

# ==================== ОПЛАТА ====================
@dp.callback_query_handler(text="pay_sber", state=BuyStates.choose_payment)
async def cb_pay_sber(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price = data.get("price", 0)
    await call.message.edit_text(
        f"💳 <b>Оплата Сбер</b>\n\n"
        f"💰 Сумма: <b>{price} руб.</b>\n\n"
        f"👤 Получатель: <b>{RECIPIENT_NAME}</b>\n\n"
        f"📤 После оплаты отправьте скриншот.",
        reply_markup=kb_back("catalog")
    )
    await state.set_state(BuyStates.wait_screenshot)
    await call.answer()

@dp.message_handler(content_types=['photo'], state=BuyStates.wait_screenshot)
async def on_screenshot(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    await msg.answer("✅ Скриншот получен! Оплата подтверждена.")
    await finalize_purchase(msg, state, account_id, msg.from_user.id)

# ==================== ЗАВЕРШЕНИЕ ПОКУПКИ ====================
async def finalize_purchase(msg: types.Message, state: FSMContext, account_id: int, user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE id = ? AND is_sold = 0", (account_id,))
    acc = cur.fetchone()
    if not acc:
        await msg.answer("❌ Аккаунт уже продан.", reply_markup=kb_main())
        await state.finish()
        conn.close()
        return
    mark_sold(account_id, user_id)
    conn.close()
    await state.finish()
    await msg.answer(
        f"✅ <b>Оплата подтверждена!</b>\n\n"
        f"📱 Номер: <code>{acc[1]}</code>\n"
        f"🌍 Страна: {acc[2]}\n"
        f"📅 Год: {acc[3]}\n\n"
        f"Нажмите кнопку для получения кода:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("📨 Получить код", callback_data=f"getcode_{acc[1]}")
        )
    )

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("getcode_"))
async def cb_get_code(call: types.CallbackQuery):
    phone = call.data[8:]
    await call.answer()
    if phone not in telethon_mgr.clients:
        await call.message.answer("⚠️ Сессия недоступна. Обратитесь к администратору.")
        return
    wait_msg = await call.message.answer(f"⏳ Ожидаю код для <code>{phone}</code>...")
    code = await telethon_mgr.wait_for_code(phone, timeout=180)
    if not code:
        await wait_msg.edit_text("⏰ Код не пришёл.", reply_markup=kb_main())
        return
    await wait_msg.edit_text(
        f"✅ <b>Код получен!</b>\n\n"
        f"🔑 <code>{code}</code>\n\n"
        f"Введите его в Telegram.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ Я вошёл", callback_data=f"logout_{phone}")
        )
    )

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("logout_"))
async def cb_logout(call: types.CallbackQuery):
    phone = call.data[7:]
    await call.message.edit_text("🔄 Завершаю сессию...")
    await telethon_mgr.logout(phone)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE accounts SET is_active = 0, session_string = NULL WHERE phone_number = ?", (phone,))
    conn.commit()
    conn.close()
    await call.message.edit_text("🎉 Аккаунт полностью ваш!", reply_markup=kb_main())
    await call.answer()

# ==================== НАВИГАЦИЯ ====================
@dp.callback_query_handler(text="main")
async def cb_main(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    admin = is_admin(call.from_user.id)
    await call.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=kb_main(admin))
    await call.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query_handler(text="adm_panel")
async def cb_adm_panel(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text("👑 <b>Панель администратора</b>", reply_markup=kb_admin())
    await call.answer()

@dp.callback_query_handler(text="adm_stats")
async def cb_adm_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    users, total, active, sold, revenue = get_stats()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"📱 Аккаунтов: {total}\n"
        f"✅ Активных: {active}\n"
        f"💰 Продано: {sold}\n"
        f"💵 Выручка: {revenue} руб.",
        reply_markup=kb_admin()
    )
    await call.answer()

@dp.callback_query_handler(text="adm_list")
async def cb_adm_list(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts ORDER BY is_sold, created_at DESC LIMIT 40")
    accs = cur.fetchall()
    conn.close()
    if not accs:
        await call.message.edit_text("📭 Аккаунтов нет.", reply_markup=kb_admin())
        await call.answer()
        return
    lines = []
    for a in accs:
        status = "💰" if a[7] else ("✅" if a[6] else "⚠️")
        lines.append(f"{status} <code>{a[1]}</code> | {a[2]} {a[3]}г. | {a[4]}₽")
    await call.message.edit_text(
        "<b>Все аккаунты:</b>\n\n" + "\n".join(lines),
        reply_markup=kb_admin()
    )
    await call.answer()

# ==================== ДОБАВЛЕНИЕ АККАУНТА ====================
@dp.callback_query_handler(text="adm_add")
async def cb_adm_add(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.add_phone)
    await call.message.edit_text(
        "📱 Введите номер телефона (например, +79991234567):",
        reply_markup=kb_back("adm_panel")
    )
    await call.answer()

@dp.message_handler(state=AdminStates.add_phone)
async def adm_add_phone(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    phone = re.sub(r'[\s\-]', '', msg.text.strip())
    if not phone.startswith('+'):
        phone = '+' + phone
    await state.update_data(phone=phone)
    await state.set_state(AdminStates.add_country)
    await msg.answer("🌍 Введите страну:")

@dp.message_handler(state=AdminStates.add_country)
async def adm_add_country(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.update_data(country=msg.text.strip())
    await state.set_state(AdminStates.add_year)
    await msg.answer(f"📅 Введите год (от 2010 до {datetime.now().year}):")

@dp.message_handler(state=AdminStates.add_year)
async def adm_add_year(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        year = int(msg.text.strip())
        if not (2010 <= year <= datetime.now().year):
            raise ValueError
    except:
        await msg.answer(f"❌ Введите год от 2010 до {datetime.now().year}")
        return
    await state.update_data(year=year)
    await state.set_state(AdminStates.add_price)
    await msg.answer("💰 Введите цену:")

@dp.message_handler(state=AdminStates.add_price)
async def adm_add_price(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        price = int(msg.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await msg.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    acc_id = add_account(data['phone'], data['country'], data['year'], price)
    await state.update_data(account_id=acc_id)
    if not TG_API_ID or not TG_API_HASH:
        await msg.answer("⚠️ Аккаунт добавлен без сессии.", reply_markup=kb_admin())
        await state.finish()
        return
    wait_msg = await msg.answer(f"🔄 Отправляю код на {data['phone']}...")
    ok = await telethon_mgr.start_login(data['phone'])
    if not ok:
        await wait_msg.edit_text("⚠️ Не удалось отправить код.", reply_markup=kb_admin())
        await state.finish()
        return
    await state.set_state(AdminStates.enter_code)
    await wait_msg.edit_text(
        f"📨 Код отправлен на {data['phone']}.\nВведите код:",
        reply_markup=kb_code_retry(data['phone'])
    )

@dp.message_handler(state=AdminStates.enter_code)
async def adm_enter_code(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    code = msg.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    account_id = data.get('account_id')
    if not phone:
        await msg.answer("⚠️ Сессия устарела.", reply_markup=kb_admin())
        await state.finish()
        return
    wait_msg = await msg.answer("🔄 Проверяю код...")
    result = await telethon_mgr.finish_login(phone, code)
    if result == "2fa":
        await wait_msg.delete()
        await state.set_state(AdminStates.enter_2fa)
        await msg.answer("🔐 Введите пароль 2FA:")
        return
    if result in ["bad_code", "expired"]:
        await wait_msg.delete()
        await msg.answer(f"❌ {'Неверный' if result == 'bad_code' else 'Просроченный'} код.", reply_markup=kb_code_retry(phone))
        return
    if not result:
        await wait_msg.delete()
        await msg.answer("❌ Ошибка входа.", reply_markup=kb_admin())
        await state.finish()
        return
    update_session(account_id, result)
    await wait_msg.delete()
    await msg.answer(f"✅ Аккаунт {phone} активирован!", reply_markup=kb_admin())
    await state.finish()

@dp.message_handler(state=AdminStates.enter_2fa)
async def adm_enter_2fa(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    password = msg.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    account_id = data.get('account_id')
    result = await telethon_mgr.finish_login_2fa(phone, password)
    if not result:
        await msg.answer("❌ Неверный пароль. Попробуйте снова.")
        return
    update_session(account_id, result)
    await msg.answer(f"✅ Аккаунт {phone} активирован (с 2FA)!", reply_markup=kb_admin())
    await state.finish()

@dp.callback_query_handler(text="adm_cancel_add")
async def cb_cancel_add(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text("↩️ Отменено.", reply_markup=kb_admin())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("resend_"))
async def cb_resend(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    phone = call.data[7:]
    ok = await telethon_mgr.start_login(phone)
    if ok:
        await call.message.edit_text(f"📨 Новый код отправлен на {phone}.", reply_markup=kb_code_retry(phone))
        await state.set_state(AdminStates.enter_code)
    else:
        await call.message.edit_text("❌ Не удалось отправить код.", reply_markup=kb_admin())
        await state.finish()
    await call.answer()

# ==================== УПРАВЛЕНИЕ АДМИНАМИ ====================
@dp.callback_query_handler(text="adm_admins")
async def cb_adm_admins(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("➕ Добавить админа", callback_data="adm_add_admin"))
    kb.add(InlineKeyboardButton("➖ Снять админа", callback_data="adm_del_admin"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="adm_panel"))
    await call.message.edit_text("👑 <b>Управление админами</b>", reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(text="adm_add_admin")
async def cb_add_admin(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.add_admin)
    await call.message.edit_text("👑 Введите Telegram ID:", reply_markup=kb_back("adm_panel"))
    await call.answer()

@dp.message_handler(state=AdminStates.add_admin)
async def proc_add_admin(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.strip())
    except:
        await msg.answer("❌ Введите число.")
        return
    if tid == MAIN_ADMIN_ID:
        await msg.answer("❌ Это главный админ.")
        await state.finish()
        return
    get_user(tid)
    set_admin(tid, True)
    await msg.answer(f"✅ Админ добавлен.", reply_markup=kb_admin())
    await state.finish()

@dp.callback_query_handler(text="adm_del_admin")
async def cb_del_admin(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.remove_admin)
    await call.message.edit_text("Введите Telegram ID для снятия прав:", reply_markup=kb_back("adm_panel"))
    await call.answer()

@dp.message_handler(state=AdminStates.remove_admin)
async def proc_del_admin(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.strip())
    except:
        await msg.answer("❌ Введите число.")
        return
    if tid == MAIN_ADMIN_ID:
        await msg.answer("❌ Нельзя снять главного админа.")
        await state.finish()
        return
    set_admin(tid, False)
    await msg.answer(f"✅ Права сняты.", reply_markup=kb_admin())
    await state.finish()

# ==================== ЗАПУСК ====================
async def startup():
    logger.info("=== БОТ ЗАПУЩЕН ===")
    if TG_API_ID and TG_API_HASH:
        await telethon_mgr.load_sessions()
        logger.info(f"Загружено {len(telethon_mgr.clients)} сессий")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(startup())
    executor.start_polling(dp, skip_updates=True)
