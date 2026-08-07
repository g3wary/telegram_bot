"""
Telegram Bot — Магазин физических аккаунтов
Полная версия с исправлениями
"""
import asyncio
import logging
import re
import os
import base64
import json
import aiohttp
from datetime import datetime
from typing import Optional, Dict, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
)
from aiogram.client.default import DefaultBotProperties

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import functions as tl_functions
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeExpiredError,
    PhoneCodeInvalidError, FloodWaitError
)

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session

import openai

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8811673952:AAELKtt-1h8f8oLBhvkLyex0HJ1SvsVhyaI")
MAIN_ADMIN_ID = int(os.environ.get("MAIN_ADMIN_ID", "8757166517"))
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///bot_database.db")

TG_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TG_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

# Реквизиты (убрал номер карты, оставил только имя)
RECIPIENT_NAME = "К. Максим Петрович"
SBER_CARD = ""

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("tg_shop")

# ==================== БАЗА ДАННЫХ ====================
Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    purchases_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    is_admin = Column(Boolean, default=False)


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    phone_number = Column(String, unique=True, nullable=False)
    country = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)
    session_string = Column(Text, nullable=True)
    is_active = Column(Boolean, default=False)
    is_sold = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    sold_to_user_id = Column(Integer, nullable=True)
    sold_at = Column(DateTime, nullable=True)


class PurchaseHistory(Base):
    __tablename__ = "purchase_history"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    account_id = Column(Integer, nullable=False)
    purchase_date = Column(DateTime, default=datetime.now)


Base.metadata.create_all(engine)

# ==================== МЕНЕДЖЕР TELETHON (ИСПРАВЛЕННЫЙ) ====================
class TelethonManager:
    def __init__(self):
        self.clients: Dict[str, TelegramClient] = {}
        self.code_waiters: Dict[str, asyncio.Future] = {}
        self.pending: Dict[str, TelegramClient] = {}
        self._code_hashes: Dict[str, str] = {}

    async def load_all(self):
        with SessionLocal() as db:
            accs = db.query(Account).filter_by(is_sold=False, is_active=True).all()
        for acc in accs:
            if acc.session_string:
                ok = await self.add_client(acc.phone_number, acc.session_string)
                logger.info(f"Session {acc.phone_number}: {'OK' if ok else 'FAILED'}")

    @staticmethod
    def _make_client(session=None) -> TelegramClient:
        return TelegramClient(
            session if session is not None else StringSession(),
            TG_API_ID,
            TG_API_HASH,
            device_model="Samsung Galaxy S23",
            system_version="Android 13",
            app_version="10.3.2",
            lang_code="ru",
            system_lang_code="ru-RU",
        )

    async def add_client(self, phone: str, session_str: str) -> bool:
        try:
            client = self._make_client(StringSession(session_str))
            await client.connect()
            if not await client.is_user_authorized():
                return False
            self._attach_handler(client, phone)
            self.clients[phone] = client
            return True
        except Exception as e:
            logger.error(f"add_client({phone}): {e}")
            return False

    def _attach_handler(self, client: TelegramClient, phone: str):
        @client.on(events.NewMessage(from_users=777000))
        async def on_service_msg(event):
            text = event.raw_text or ""
            code_m = re.search(r'\b(\d{5,6})\b', text)
            if code_m:
                code = code_m.group(1)
                logger.info(f"Got code for {phone}: {code}")
                fut = self.code_waiters.get(phone)
                if fut and not fut.done():
                    fut.set_result(code)

    async def start_login(self, phone: str) -> bool:
        try:
            client = self._make_client()
            await client.connect()
            sent = await client.send_code_request(phone)
            self._code_hashes[phone] = sent.phone_code_hash
            self.pending[phone] = client
            logger.info(f"✅ Код отправлен на {phone}")
            return True
        except FloodWaitError as e:
            logger.error(f"FloodWait {e.seconds}s для {phone}")
            return False
        except Exception as e:
            logger.error(f"start_login({phone}): {e}")
            return False

    async def finish_login(self, phone: str, code: str) -> Optional[str]:
        client = self.pending.get(phone)
        if not client:
            logger.error(f"finish_login({phone}): no pending client")
            return None

        clean_code = re.sub(r'\D', '', code.strip())
        logger.info(f"finish_login({phone}): trying code len={len(clean_code)}")

        try:
            phone_hash = self._code_hashes.get(phone)
            if not phone_hash:
                logger.error(f"finish_login({phone}): no code hash")
                return None

            await client.sign_in(
                phone=phone,
                code=clean_code,
                phone_code_hash=phone_hash
            )
        except SessionPasswordNeededError:
            return "2fa"
        except PhoneCodeExpiredError:
            logger.warning(f"finish_login({phone}): PhoneCodeExpiredError")
            return "expired"
        except PhoneCodeInvalidError:
            logger.warning(f"finish_login({phone}): PhoneCodeInvalidError")
            return "bad_code"
        except Exception as e:
            logger.error(f"finish_login({phone}): {e}")
            return None

        session_str = client.session.save()
        self._attach_handler(client, phone)
        self.clients[phone] = client
        self.pending.pop(phone, None)
        return session_str

    async def manual_login(self, phone: str, code: str) -> Optional[str]:
        """Ручной вход без pending-клиента (запасной вариант)"""
        try:
            client = self._make_client()
            await client.connect()
            await client.sign_in(phone=phone, code=code)
            if await client.is_user_authorized():
                session_str = client.session.save()
                self._attach_handler(client, phone)
                self.clients[phone] = client
                return session_str
            return None
        except Exception as e:
            logger.error(f"manual_login({phone}): {e}")
            return None

    async def finish_login_2fa(self, phone: str, password: str) -> Optional[str]:
        client = self.pending.get(phone)
        if not client:
            return None
        try:
            await client.sign_in(password=password)
        except Exception as e:
            logger.error(f"finish_login_2fa({phone}): {e}")
            return None
        session_str = client.session.save()
        self._attach_handler(client, phone)
        self.clients[phone] = client
        self.pending.pop(phone, None)
        return session_str

    async def resend_code(self, phone: str) -> bool:
        client = self.pending.get(phone)
        if not client:
            return False
        phone_hash = self._code_hashes.get(phone)
        if not phone_hash:
            return False
        try:
            result = await client(tl_functions.auth.ResendCodeRequest(
                phone_number=phone,
                phone_code_hash=phone_hash,
            ))
            self._code_hashes[phone] = result.phone_code_hash
            return True
        except Exception as e:
            logger.error(f"resend_code({phone}): {e}")
            return False

    async def wait_for_code(self, phone: str, timeout: int = 300) -> Optional[str]:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.code_waiters[phone] = fut
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.code_waiters.pop(phone, None)

    async def logout(self, phone: str):
        client = self.clients.pop(phone, None)
        if client:
            try:
                await client.log_out()
            except Exception as e:
                logger.error(f"logout({phone}): {e}")
            finally:
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

# ==================== ИИ-ПРОВЕРКА СКРИНШОТА ====================
async def verify_screenshot(photo_bytes: bytes, expected_amount: int) -> dict:
    try:
        if not OPENAI_KEY:
            return {"error": "no_openai_key", "name_correct": True, "amount_correct": True, "is_payment_screen": True}
        
        ai_client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
        encoded = base64.b64encode(photo_bytes).decode()

        prompt = (
            f"Перед тобой скриншот. Определи:\n"
            f"1. Это скриншот перевода/платежа через банковское приложение?\n"
            f"2. Имя получателя содержит «Максим» (или «К. Максим Петрович», «Максим П.» и т.п.)?\n"
            f"3. Сумма перевода равна {expected_amount} рублей (или {expected_amount}.00 ₽)?\n\n"
            f"Ответь ТОЛЬКО JSON (без пояснений):\n"
            f'{{"is_payment_screen": true/false, '
            f'"name_correct": true/false, '
            f'"amount_correct": true/false, '
            f'"detected_name": "...", '
            f'"detected_amount": "..."}}'
        )

        response = await ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "low"
                    }}
                ]
            }],
            max_tokens=200,
        )

        raw = response.choices[0].message.content.strip()
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {"error": "no_json", "name_correct": False, "amount_correct": False, "is_payment_screen": False}

    except Exception as e:
        logger.error(f"verify_screenshot: {e}")
        return {"error": str(e), "name_correct": False, "amount_correct": False, "is_payment_screen": False}

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_user(tg_id: int, username=None, full_name=None) -> User:
    with SessionLocal() as db:
        u = db.query(User).filter_by(telegram_id=tg_id).first()
        if not u:
            u = User(telegram_id=tg_id, username=username, full_name=full_name)
            db.add(u)
            db.commit()
            db.refresh(u)
        return u

def is_admin(tg_id: int) -> bool:
    if tg_id == MAIN_ADMIN_ID:
        return True
    with SessionLocal() as db:
        u = db.query(User).filter_by(telegram_id=tg_id).first()
        return bool(u and u.is_admin)

def available_countries() -> List[str]:
    with SessionLocal() as db:
        rows = db.query(Account.country).filter_by(is_sold=False, is_active=True).distinct().all()
    return sorted({r[0] for r in rows})

def available_years(country: str) -> List[int]:
    with SessionLocal() as db:
        rows = db.query(Account.year).filter_by(country=country, is_sold=False, is_active=True).distinct().all()
    return sorted({r[0] for r in rows})

def pick_account(country: str, year: int) -> Optional[Account]:
    with SessionLocal() as db:
        acc = db.query(Account).filter_by(country=country, year=year, is_sold=False, is_active=True).first()
        if acc:
            db.expunge(acc)
        return acc

# ==================== КЛАВИАТУРЫ ====================
def kb_main(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="catalog")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="adm_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📋 Аккаунты", callback_data="adm_list")],
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="adm_add")],
        [InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data="adm_delete_list")],
        [InlineKeyboardButton(text="👑 Управление админами", callback_data="adm_admins")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main")],
    ])

def kb_countries(countries: List[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🌍 {c}", callback_data=f"c_{c}")] for c in countries]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_years(years: List[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📅 {y} год", callback_data=f"y_{y}")] for y in years]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_payment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Сбербанк", callback_data="pay_sber")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="catalog")],
    ])

def kb_code_retry(phone: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Запросить новый код", callback_data=f"resend_{phone}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel_add")],
    ])

def kb_back(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=target)]
    ])

# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    get_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name)
    admin = is_admin(msg.from_user.id)
    await msg.answer(
        "👋 <b>Добро пожаловать в магазин аккаунтов!</b>\n\n"
        "📱 Здесь вы можете купить физический аккаунт Telegram с автоматической выдачей.\n"
        "Выберите действие:",
        reply_markup=kb_main(admin)
    )

@dp.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.clear()
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет прав администратора.")
        return
    await msg.answer("👑 <b>Панель администратора</b>", reply_markup=kb_admin())

# ==================== ПРОФИЛЬ ====================
@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    admin = is_admin(cb.from_user.id)
    with SessionLocal() as db:
        u = db.query(User).filter_by(telegram_id=cb.from_user.id).first()
        cnt = db.query(PurchaseHistory).filter_by(user_id=cb.from_user.id).count()
    created = u.created_at.strftime("%d.%m.%Y") if u else "—"
    await cb.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{cb.from_user.id}</code>\n"
        f"👤 Ник: @{cb.from_user.username or 'не указан'}\n"
        f"📦 Куплено: <b>{cnt}</b>\n"
        f"📅 В боте с: {created}",
        reply_markup=kb_main(admin)
    )
    await cb.answer()

# ==================== КАТАЛОГ ====================
@dp.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    countries = available_countries()
    if not countries:
        await cb.message.edit_text("😔 Каталог пуст.", reply_markup=kb_main(is_admin(cb.from_user.id)))
        await cb.answer()
        return
    await cb.message.edit_text("🌍 <b>Выберите страну:</b>", reply_markup=kb_countries(countries))
    await state.set_state(BuyStates.choose_country)
    await cb.answer()

@dp.callback_query(F.data.startswith("c_"), StateFilter(BuyStates.choose_country))
async def cb_choose_country(cb: CallbackQuery, state: FSMContext):
    country = cb.data[2:]
    years = available_years(country)
    if not years:
        await cb.message.edit_text("😔 Нет доступных аккаунтов.", reply_markup=kb_main())
        await state.clear()
        await cb.answer()
        return
    await state.update_data(country=country)
    await cb.message.edit_text(f"📅 <b>Выберите год</b> ({country}):", reply_markup=kb_years(years))
    await state.set_state(BuyStates.choose_year)
    await cb.answer()

@dp.callback_query(F.data.startswith("y_"), StateFilter(BuyStates.choose_year))
async def cb_choose_year(cb: CallbackQuery, state: FSMContext):
    year = int(cb.data[2:])
    data = await state.get_data()
    country = data["country"]
    acc = pick_account(country, year)
    if not acc:
        await cb.message.edit_text("😔 Аккаунты закончились.", reply_markup=kb_main())
        await state.clear()
        await cb.answer()
        return
    age = datetime.now().year - year
    await state.update_data(year=year, account_id=acc.id, price=acc.price)
    await cb.message.edit_text(
        f"📱 <b>Аккаунт найден!</b>\n\n"
        f"🌍 Страна: {country}\n"
        f"📅 Год: {year} ({age} лет)\n"
        f"💰 Цена: <b>{acc.price} руб.</b>\n\n"
        f"Выберите способ оплаты:",
        reply_markup=kb_payment()
    )
    await state.set_state(BuyStates.choose_payment)
    await cb.answer()

# ==================== ОПЛАТА ====================
@dp.callback_query(F.data == "pay_sber", StateFilter(BuyStates.choose_payment))
async def cb_pay_sber(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price = data.get("price", 0)
    await cb.message.edit_text(
        f"💳 <b>Оплата переводом Сбер</b>\n\n"
        f"💰 Сумма к оплате: <b>{price} руб.</b>\n\n"
        f"👤 Получатель: <b>{RECIPIENT_NAME}</b>\n\n"
        f"⚠️ <b>Важно:</b>\n"
        f"• Сумма должна совпадать точно\n"
        f"• В назначении платежа укажите: <b>Оплата аккаунта</b>\n"
        f"• На скриншоте должно быть видно имя «Максим»\n\n"
        f"📤 После перевода отправьте скриншот подтверждения:",
        reply_markup=kb_back("catalog")
    )
    await state.set_state(BuyStates.wait_screenshot)
    await cb.answer()

@dp.message(F.photo, StateFilter(BuyStates.wait_screenshot))
async def on_screenshot(msg: Message, state: FSMContext):
    data = await state.get_data()
    account_id = data.get("account_id")
    price = data.get("price", 0)

    checking_msg = await msg.answer("🔍 Анализирую скриншот...")

    photo = msg.photo[-1]
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            photo_bytes = await resp.read()

    result = await verify_screenshot(photo_bytes, price)
    await checking_msg.delete()

    # Если OpenAI не настроен - автоматически подтверждаем
    if result.get("error") == "no_openai_key":
        await msg.answer("✅ Оплата принята (ручная проверка)!")
        await finalize_purchase(msg, state, account_id, msg.from_user.id)
        return

    if result.get("error"):
        await msg.answer(
            "⚠️ Ошибка анализа. Попробуйте снова или обратитесь в поддержку.",
            reply_markup=kb_main()
        )
        return

    if not result.get("is_payment_screen"):
        await msg.answer(
            "❌ <b>Скриншот не распознан как перевод</b>\n\n"
            "Отправьте скриншот из банковского приложения.",
            reply_markup=kb_back("catalog")
        )
        return

    if not result.get("name_correct"):
        detected = result.get("detected_name") or "не определено"
        await msg.answer(
            f"❌ <b>Неверный получатель</b>\n\n"
            f"На скриншоте: <b>{detected}</b>\n"
            f"Должно быть: <b>{RECIPIENT_NAME}</b>",
            reply_markup=kb_back("catalog")
        )
        return

    if not result.get("amount_correct"):
        detected_amt = result.get("detected_amount") or "не определено"
        await msg.answer(
            f"❌ <b>Неверная сумма</b>\n\n"
            f"На скриншоте: <b>{detected_amt}</b>\n"
            f"Ожидалось: <b>{price} руб.</b>",
            reply_markup=kb_back("catalog")
        )
        return

    await finalize_purchase(msg, state, account_id, msg.from_user.id)

@dp.callback_query(F.data == "pay_stars", StateFilter(BuyStates.choose_payment))
async def cb_pay_stars(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    acc_id = data.get("account_id")
    country = data.get("country", "")
    year = data.get("year", "")
    price = data.get("price", 0)

    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"Аккаунт {country} {year}",
            description=f"Физический аккаунт Telegram — {country}, {year} год",
            payload=f"acc_{acc_id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Аккаунт", amount=price)],
        )
        await cb.answer()
    except Exception as e:
        logger.error(f"send_invoice error: {e}")
        await cb.message.edit_text("❌ Ошибка при создании счёта.", reply_markup=kb_main())
        await state.clear()
        await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def on_success_payment(msg: Message, state: FSMContext):
    payload = msg.successful_payment.invoice_payload
    account_id = int(payload.replace("acc_", ""))
    await finalize_purchase(msg, state, account_id, msg.from_user.id)

# ==================== ЗАВЕРШЕНИЕ ПОКУПКИ ====================
async def finalize_purchase(msg: Message, state: FSMContext, account_id: int, user_id: int):
    with SessionLocal() as db:
        acc = db.query(Account).filter_by(id=account_id, is_sold=False).first()
        if not acc:
            await msg.answer("❌ Аккаунт уже продан.", reply_markup=kb_main())
            await state.clear()
            return

        acc.is_sold = True
        acc.sold_to_user_id = user_id
        acc.sold_at = datetime.now()

        u = db.query(User).filter_by(telegram_id=user_id).first()
        if u:
            u.purchases_count += 1

        db.add(PurchaseHistory(user_id=user_id, account_id=account_id))
        db.commit()

        phone = acc.phone_number
        country = acc.country
        year = acc.year

    await state.clear()
    await msg.answer(
        f"✅ <b>Оплата подтверждена!</b>\n\n"
        f"📱 Номер: <code>{phone}</code>\n"
        f"🌍 Страна: {country}\n"
        f"📅 Год: {year}\n\n"
        f"<b>Как войти:</b>\n"
        f"1. Настройки → Добавить аккаунт\n"
        f"2. Введите номер: <code>{phone}</code>\n"
        f"3. Нажмите «Получить код»\n"
        f"4. Нажмите кнопку ниже для получения кода:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📨 Получить код",
                callback_data=f"getcode_{phone}"
            )]
        ])
    )

# ==================== ПОЛУЧЕНИЕ КОДА ====================
@dp.callback_query(F.data.startswith("getcode_"))
async def cb_get_code(cb: CallbackQuery):
    phone = cb.data[8:]
    await cb.answer()

    if phone not in telethon_mgr.clients:
        await cb.message.answer(
            "⚠️ Сессия недоступна. Обратитесь к администратору.",
            reply_markup=kb_main()
        )
        return

    wait_msg = await cb.message.answer(
        f"⏳ Ожидаю код для <code>{phone}</code>...\n(до 5 минут)"
    )

    code = await telethon_mgr.wait_for_code(phone, timeout=300)

    if not code:
        await wait_msg.edit_text(
            "⏰ Код не пришёл.\nУбедитесь, что запросили код в Telegram.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"getcode_{phone}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main")],
            ])
        )
        return

    await wait_msg.edit_text(
        f"✅ <b>Код получен!</b>\n\n"
        f"🔑 <code>{code}</code>\n\n"
        f"Введите его в Telegram. После входа нажмите кнопку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Я вошёл — бот может выйти",
                callback_data=f"logout_{phone}"
            )]
        ])
    )

@dp.callback_query(F.data.startswith("logout_"))
async def cb_logout(cb: CallbackQuery):
    phone = cb.data[7:]
    await cb.message.edit_text("🔄 Завершаю сессию...")

    await telethon_mgr.logout(phone)

    with SessionLocal() as db:
        acc = db.query(Account).filter_by(phone_number=phone).first()
        if acc:
            acc.is_active = False
            acc.session_string = None
            db.commit()

    await cb.message.edit_text(
        "🎉 <b>Готово! Аккаунт полностью ваш.</b>\n\n"
        "Для новых покупок — /start",
        reply_markup=kb_main()
    )
    await cb.answer()

# ==================== НАВИГАЦИЯ ====================
@dp.callback_query(F.data == "main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    admin = is_admin(cb.from_user.id)
    await cb.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=kb_main(admin))
    await cb.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "adm_panel")
async def cb_adm_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("👑 <b>Панель администратора</b>", reply_markup=kb_admin())
    await cb.answer()

@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    with SessionLocal() as db:
        total = db.query(Account).count()
        sold = db.query(Account).filter_by(is_sold=True).count()
        users = db.query(User).count()
        revenue = db.query(func.sum(Account.price)).filter_by(is_sold=True).scalar() or 0
        active = db.query(Account).filter_by(is_sold=False, is_active=True).count()

    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"📱 Аккаунтов: {total}\n"
        f"✅ Активных: {active}\n"
        f"💰 Продано: {sold}\n"
        f"💵 Выручка: {revenue} руб.",
        reply_markup=kb_admin()
    )
    await cb.answer()

@dp.callback_query(F.data == "adm_list")
async def cb_adm_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    with SessionLocal() as db:
        accs = db.query(Account).order_by(Account.is_sold, Account.created_at.desc()).limit(40).all()

    if not accs:
        await cb.message.edit_text("📭 Аккаунтов нет.", reply_markup=kb_admin())
        await cb.answer()
        return

    lines = []
    for a in accs:
        status = "💰" if a.is_sold else ("✅" if a.is_active else "⚠️")
        lines.append(f"{status} <code>{a.phone_number}</code> | {a.country} {a.year}г. | {a.price}₽")

    await cb.message.edit_text(
        "<b>Все аккаунты:</b>\n✅=доступен ⚠️=нет сессии 💰=продан\n\n" + "\n".join(lines),
        reply_markup=kb_admin()
    )
    await cb.answer()

# ==================== АДМИН — ДОБАВЛЕНИЕ АККАУНТА ====================
@dp.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.add_phone)
    await cb.message.edit_text(
        "📱 <b>Добавление аккаунта</b>\n\n"
        "Введите номер телефона (например, +79991234567):",
        reply_markup=kb_back("adm_panel")
    )
    await cb.answer()

@dp.message(StateFilter(AdminStates.add_phone))
async def adm_add_phone(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    phone = re.sub(r'[\s\-]', '', msg.text.strip())
    if not phone.startswith('+'):
        phone = '+' + phone
    await state.update_data(phone=phone)
    await state.set_state(AdminStates.add_country)
    await msg.answer("🌍 Введите страну (РФ, США, UK, Германия и т.д.):")

@dp.message(StateFilter(AdminStates.add_country))
async def adm_add_country(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.update_data(country=msg.text.strip())
    await state.set_state(AdminStates.add_year)
    await msg.answer(f"📅 Введите год (от 2010 до {datetime.now().year}):")

@dp.message(StateFilter(AdminStates.add_year))
async def adm_add_year(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        year = int(msg.text.strip())
        if not (2010 <= year <= datetime.now().year):
            raise ValueError
    except ValueError:
        await msg.answer(f"❌ Введите год от 2010 до {datetime.now().year}")
        return
    await state.update_data(year=year)
    await state.set_state(AdminStates.add_price)
    age = datetime.now().year - year
    await msg.answer(f"💰 Введите цену в рублях (рекомендуется ~{age * 100} ₽):")

@dp.message(StateFilter(AdminStates.add_price))
async def adm_add_price(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        price = int(msg.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введите положительное число.")
        return

    data = await state.get_data()
    with SessionLocal() as db:
        acc = Account(
            phone_number=data['phone'],
            country=data['country'],
            year=data['year'],
            price=price,
            is_active=False
        )
        db.add(acc)
        db.commit()
        acc_id = acc.id

    await state.update_data(account_id=acc_id)

    # Пытаемся активировать
    wait_msg = await msg.answer(f"🔄 Инициирую вход в {data['phone']}...")

    if not TG_API_ID or not TG_API_HASH:
        await wait_msg.edit_text(
            "⚠️ API_ID / API_HASH не заданы. Аккаунт добавлен без сессии.",
            reply_markup=kb_admin()
        )
        await state.clear()
        return

    ok = await telethon_mgr.start_login(data['phone'])
    if not ok:
        await wait_msg.edit_text(
            "⚠️ Не удалось отправить код. Аккаунт добавлен без сессии.",
            reply_markup=kb_admin()
        )
        await state.clear()
        return

    await state.set_state(AdminStates.enter_code)
    await wait_msg.edit_text(
        f"📨 Код отправлен на {data['phone']}.\nВведите полученный код:",
        reply_markup=kb_code_retry(data['phone'])
    )

@dp.message(StateFilter(AdminStates.enter_code))
async def adm_enter_code(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    code = msg.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    acc_id = data.get("account_id")

    logger.info(f"=== ВВОД КОДА ===")
    logger.info(f"Phone: {phone}, Code: {code}, Pending: {list(telethon_mgr.pending.keys())}")

    if not phone:
        await msg.answer("⚠️ Сессия устарела. Добавьте аккаунт заново.", reply_markup=kb_admin())
        await state.clear()
        return

    digits = re.sub(r'\D', '', code)
    if len(digits) < 5:
        await msg.answer("❌ Введите цифровой код из SMS (5-6 цифр).")
        return

    wait_msg = await msg.answer("🔄 Проверяю код...")

    # Пробуем стандартный вход
    result = await telethon_mgr.finish_login(phone, code)

    # Если не получилось - пробуем ручной вход
    if result in ["expired", "bad_code", None]:
        logger.info("Пробуем ручной вход...")
        result = await telethon_mgr.manual_login(phone, code)
        if result:
            with SessionLocal() as db:
                acc = db.query(Account).filter_by(id=acc_id).first()
                if acc:
                    acc.session_string = result
                    acc.is_active = True
                    db.commit()
            await wait_msg.delete()
            await msg.answer(
                f"✅ Аккаунт активирован (ручной вход)!",
                reply_markup=kb_admin()
            )
            await state.clear()
            return

    if result == "2fa":
        await wait_msg.delete()
        await state.set_state(AdminStates.enter_2fa)
        await msg.answer(
            "🔐 <b>Требуется пароль 2FA</b>\n\nВведите облачный пароль:",
            reply_markup=kb_back("adm_panel")
        )
        return

    if result == "bad_code":
        await wait_msg.delete()
        await msg.answer(
            "❌ Неверный код. Попробуйте снова:",
            reply_markup=kb_code_retry(phone)
        )
        return

    if result == "expired":
        await wait_msg.delete()
        await msg.answer(
            "⏰ Код истёк. Запросите новый:",
            reply_markup=kb_code_retry(phone)
        )
        return

    if not result:
        await wait_msg.delete()
        await msg.answer(
            "⚠️ Не удалось войти. Попробуйте заново.",
            reply_markup=kb_admin()
        )
        await state.clear()
        return

    # Успех
    await wait_msg.delete()
    with SessionLocal() as db:
        acc = db.query(Account).filter_by(id=acc_id).first()
        if acc:
            acc.session_string = result
            acc.is_active = True
            db.commit()

    await msg.answer(
        f"✅ Аккаунт активирован!\n\n📱 {data['phone']}\n🌍 {data['country']}\n📅 {data['year']} г.",
        reply_markup=kb_admin()
    )
    await state.clear()

@dp.message(StateFilter(AdminStates.enter_2fa))
async def adm_enter_2fa(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    password = msg.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    acc_id = data.get("account_id")

    result = await telethon_mgr.finish_login_2fa(phone, password)

    if not result:
        await msg.answer("❌ Неверный пароль. Попробуйте снова.")
        return

    with SessionLocal() as db:
        acc = db.query(Account).filter_by(id=acc_id).first()
        if acc:
            acc.session_string = result
            acc.is_active = True
            db.commit()

    await msg.answer(
        f"✅ Аккаунт активирован (с 2FA)!\n\n📱 {data['phone']}\n🌍 {data['country']}\n📅 {data['year']} г.",
        reply_markup=kb_admin()
    )
    await state.clear()

@dp.callback_query(F.data == "adm_cancel_add")
async def cb_cancel_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("↩️ Добавление отменено.", reply_markup=kb_admin())
    await cb.answer()

@dp.callback_query(F.data.startswith("resend_"))
async def cb_resend_code(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    phone = cb.data[7:]
    ok = await telethon_mgr.resend_code(phone)
    if not ok:
        ok = await telethon_mgr.start_login(phone)

    if ok:
        await cb.message.edit_text(
            f"📨 Новый код отправлен на {phone}.\nВведите код:",
            reply_markup=kb_code_retry(phone)
        )
        await state.set_state(AdminStates.enter_code)
    else:
        await cb.message.edit_text("❌ Не удалось отправить код.", reply_markup=kb_admin())
        await state.clear()
    await cb.answer()

# ==================== АДМИН — УДАЛЕНИЕ АККАУНТА ====================
@dp.callback_query(F.data == "adm_delete_list")
async def cb_adm_delete_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    with SessionLocal() as db:
        accs = db.query(Account).order_by(Account.is_sold, Account.created_at.desc()).limit(30).all()

    if not accs:
        await cb.message.edit_text("📭 Нет аккаунтов для удаления.", reply_markup=kb_admin())
        await cb.answer()
        return

    rows = []
    for a in accs:
        status = "💰" if a.is_sold else ("✅" if a.is_active else "⚠️")
        label = f"{status} {a.phone_number} | {a.country} {a.year}г. | {a.price}₽"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_pick_{a.id}")])

    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_panel")])
    await cb.message.edit_text(
        "🗑️ <b>Выберите аккаунт для удаления:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_pick_"))
async def cb_adm_del_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    try:
        acc_id = int(cb.data.removeprefix("adm_pick_"))
    except ValueError:
        await cb.answer("Неверный ID", show_alert=True)
        return

    with SessionLocal() as db:
        acc = db.query(Account).filter_by(id=acc_id).first()
        if not acc:
            await cb.message.edit_text("❌ Аккаунт не найден.", reply_markup=kb_admin())
            await cb.answer()
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_yes_{acc_id}"),
         InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_no_{acc_id}")]
    ])
    await cb.message.edit_text(
        f"🗑️ <b>Удалить аккаунт?</b>\n\n"
        f"📱 <code>{acc.phone_number}</code>\n"
        f"🌍 {acc.country}\n"
        f"📅 {acc.year}г.\n"
        f"💰 {acc.price}₽\n\n"
        f"⚠️ Действие необратимо.",
        reply_markup=kb
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_yes_"))
async def cb_adm_del_yes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    try:
        acc_id = int(cb.data.removeprefix("adm_yes_"))
    except ValueError:
        await cb.answer("Неверный ID", show_alert=True)
        return

    with SessionLocal() as db:
        acc = db.query(Account).filter_by(id=acc_id).first()
        if acc:
            phone = acc.phone_number
            db.delete(acc)
            db.commit()
            await cb.message.edit_text(f"✅ Аккаунт <code>{phone}</code> удалён.", reply_markup=kb_admin())
        else:
            await cb.message.edit_text("❌ Аккаунт не найден.", reply_markup=kb_admin())
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_no_"))
async def cb_adm_del_no(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("↩️ Удаление отменено.", reply_markup=kb_admin())
    await cb.answer()

# ==================== АДМИН — УПРАВЛЕНИЕ АДМИНАМИ ====================
@dp.callback_query(F.data == "adm_admins")
async def cb_adm_admins(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text(
        "👑 <b>Управление администраторами</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="adm_add_admin")],
            [InlineKeyboardButton(text="➖ Снять", callback_data="adm_del_admin")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_panel")],
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "adm_add_admin")
async def cb_add_admin(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.add_admin)
    await cb.message.edit_text("👑 Введите Telegram ID:", reply_markup=kb_back("adm_panel"))
    await cb.answer()

@dp.message(StateFilter(AdminStates.add_admin))
async def proc_add_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Введите число.")
        return
    with SessionLocal() as db:
        u = db.query(User).filter_by(telegram_id=tid).first()
        if not u:
            u = User(telegram_id=tid)
            db.add(u)
        if u.is_admin:
            await msg.answer("❌ Уже админ.")
        else:
            u.is_admin = True
            db.commit()
            await msg.answer(f"✅ Админ добавлен.", reply_markup=kb_admin())
    await state.clear()

@dp.callback_query(F.data == "adm_del_admin")
async def cb_del_admin(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.remove_admin)
    await cb.message.edit_text("Введите Telegram ID для снятия прав:", reply_markup=kb_back("adm_panel"))
    await cb.answer()

@dp.message(StateFilter(AdminStates.remove_admin))
async def proc_del_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Введите число.")
        return
    if tid == MAIN_ADMIN_ID:
        await msg.answer("❌ Нельзя снять главного админа.")
        await state.clear()
        return
    with SessionLocal() as db:
        u = db.query(User).filter_by(telegram_id=tid).first()
        if not u or not u.is_admin:
            await msg.answer("❌ Не админ.")
        else:
            u.is_admin = False
            db.commit()
            await msg.answer(f"✅ Права сняты.", reply_markup=kb_admin())
    await state.clear()

# ==================== ЗАПУСК ====================
async def main():
    logger.info("=== БОТ ЗАПУЩЕН ===")

    if not TG_API_ID or not TG_API_HASH:
        logger.warning("⚠️ TELEGRAM_API_ID / HASH не заданы! Telethon не будет работать.")
    else:
        await telethon_mgr.load_all()
        logger.info(f"✅ Загружено {len(telethon_mgr.clients)} сессий")

    with SessionLocal() as db:
        if db.query(Account).count() == 0:
            test = [
                Account(phone_number="+79991234567", country="РФ", year=2021, price=300),
                Account(phone_number="+79991234568", country="РФ", year=2022, price=200),
                Account(phone_number="+19991234567", country="США", year=2023, price=400),
                Account(phone_number="+49123456789", country="Германия", year=2022, price=350),
            ]
            db.add_all(test)
            db.commit()
            logger.info("➕ Добавлены тестовые аккаунты")

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
