"""
Telegram-магазин физических аккаунтов.
aiogram 3 + SQLAlchemy 2 (async) + Tesseract OCR + Telethon.

Сценарий покупки:
  1) /start → Соглашение
  2) Каталог: страна → год
  3) Оплата: СБП (скрин) или Stars
  4) Бот проверяет → выдаёт телефон
  5) Telethon-клиент, сидящий на этом аккаунте, ловит входящий код
  6) Бот шлёт код покупателю
  7) Покупатель вводит код у себя → входит
  8) Бот выходит с аккаунта (log_out) и удаляет его из каталога

Запуск:
  pip install -r requirements.txt
  python main.py
"""
import os
import io
import re
import logging
import asyncio
from datetime import datetime
from typing import Optional

from PIL import Image
import pytesseract

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import BigInteger, String, Integer, DateTime, Boolean, select, func
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
)

load_dotenv()


# ══════════════════════════════════════════════
#  КОНФИГ
# ══════════════════════════════════════════════
def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"❌ Переменная {name} не задана")
    return v


def _int_env(name: str, default: Optional[str] = None) -> int:
    return int(_env(name, default))


def _float_env(name: str, default: Optional[str] = None) -> float:
    return float(_env(name, default))


BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_ID = _int_env("ADMIN_ID")
SBER_CARD = _env("SBER_CARD")
SBER_RECIPIENT = _env("SBER_RECIPIENT")
SBER_BANK = _env("SBER_BANK", "Сбер Банк")
SUPPORT_USERNAME = _env("SUPPORT_USERNAME", "sadgewary")
API_ID = _int_env("API_ID", "0")
API_HASH = _env("API_HASH", "")
DB_URL = _env("DB_URL", "sqlite+aiosqlite:////data/bot.db")
STARS_TO_RUB = _float_env("STARS_TO_RUB", "1.4")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("shop")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ══════════════════════════════════════════════
#  БД — модели
# ══════════════════════════════════════════════
engine = create_async_engine(DB_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    full_name: Mapped[str] = mapped_column(String(128), default="")
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    spent_rub: Mapped[int] = mapped_column(Integer, default=0)
    spent_stars: Mapped[int] = mapped_column(Integer, default=0)
    tos_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    country: Mapped[str] = mapped_column(String(64))
    flag: Mapped[str] = mapped_column(String(8), default="🌍")
    year: Mapped[int] = mapped_column(Integer)
    age: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[int] = mapped_column(Integer)
    session_str: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[int] = mapped_column(Integer)
    phone: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(64))
    year: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[int] = mapped_column(Integer)
    price_stars: Mapped[int] = mapped_column(Integer)
    pay_method: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ══════════════════════════════════════════════
#  ХЕЛПЕРЫ БД
# ══════════════════════════════════════════════
async def get_or_create_user(s: AsyncSession, tg) -> User:
    u = await s.get(User, tg.id)
    if not u:
        u = User(
            id=tg.id,
            username=tg.username or "",
            full_name=tg.full_name or "",
        )
        s.add(u)
        await s.commit()
    return u


async def is_admin(tg_id: int) -> bool:
    if tg_id == ADMIN_ID:
        return True
    async with SessionLocal() as s:
        return await s.get(Admin, tg_id) is not None


async def add_admin(tg_id: int, username: str = ""):
    async with SessionLocal() as s:
        if not await s.get(Admin, tg_id):
            s.add(Admin(id=tg_id, username=username))
            await s.commit()


async def remove_admin(tg_id: int):
    async with SessionLocal() as s:
        a = await s.get(Admin, tg_id)
        if a and tg_id != ADMIN_ID:
            await s.delete(a)
            await s.commit()


async def list_admins() -> list[Admin]:
    async with SessionLocal() as s:
        r = await s.execute(select(Admin).order_by(Admin.added_at))
        return list(r.scalars())


async def all_accounts() -> list[Account]:
    async with SessionLocal() as s:
        r = await s.execute(select(Account).order_by(Account.country, Account.year.desc()))
        return list(r.scalars())


async def get_account(acc_id: int) -> Optional[Account]:
    async with SessionLocal() as s:
        return await s.get(Account, acc_id)


async def add_account(phone: str, country: str, flag: str, year: int, age: int, price: int, session_str: str = "") -> Optional[Account]:
    async with SessionLocal() as s:
        if await s.get(Account, phone):
            return None
        a = Account(
            phone=phone, country=country, flag=flag,
            year=year, age=age, price=price, session_str=session_str,
        )
        s.add(a)
        await s.commit()
        return a


async def update_account(acc_id: int, **fields) -> bool:
    async with SessionLocal() as s:
        a = await s.get(Account, acc_id)
        if not a:
            return False
        for k, v in fields.items():
            if v is not None and hasattr(a, k):
                setattr(a, k, v)
        await s.commit()
        return True


async def delete_account(acc_id: int) -> Optional[Account]:
    async with SessionLocal() as s:
        a = await s.get(Account, acc_id)
        if not a:
            return None
        await s.delete(a)
        await s.commit()
        return a


async def record_purchase(user_id: int, acc: Account, pay_method: str) -> Purchase:
    stars = rub_to_stars(acc.price)
    async with SessionLocal() as s:
        p = Purchase(
            user_id=user_id,
            account_id=acc.id,
            phone=acc.phone,
            country=acc.country,
            year=acc.year,
            price_rub=acc.price,
            price_stars=stars,
            pay_method=pay_method,
        )
        s.add(p)
        u = await s.get(User, user_id)
        if u:
            u.purchases += 1
            if pay_method == "sber":
                u.spent_rub += acc.price
            else:
                u.spent_stars += stars
        await s.commit()
    return p


async def stats() -> dict:
    async with SessionLocal() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        avail = (await s.execute(select(func.count(Account.id)))).scalar() or 0
        sold_rub = (await s.execute(
            select(func.coalesce(func.sum(Purchase.price_rub), 0))
        )).scalar() or 0
        sold_stars = (await s.execute(
            select(func.coalesce(func.sum(Purchase.price_stars), 0))
        )).scalar() or 0
    sold = 0
    async with SessionLocal() as s:
        sold = (await s.execute(select(func.count(Purchase.id)))).scalar() or 0
    return {
        "users": users,
        "available": avail,
        "sold": sold,
        "total": avail + sold,
        "money_rub": int(sold_rub),
        "money_stars": int(sold_stars),
    }


def rub_to_stars(rub: int) -> int:
    return max(1, round(rub / STARS_TO_RUB))


# ══════════════════════════════════════════════
#  TELETHON — реальные сессии на аккаунтах
# ══════════════════════════════════════════════
# Пул клиентов: phone -> TelegramClient
telethon_clients: dict[str, TelegramClient] = {}
# Кто сейчас ждёт код от какого аккаунта: phone -> user_id
telethon_waiting: dict[str, int] = {}
# Аккаунт-источник кода: user_id -> phone
user_waiting_for: dict[int, str] = {}


def get_telethon_client(phone: str, session_str: str = "") -> TelegramClient:
    if phone in telethon_clients:
        return telethon_clients[phone]
    client = TelegramClient(
        StringSession(session_str) if session_str else StringSession(),
        api_id=API_ID,
        api_hash=API_HASH,
        device_model="ShopBot",
        system_version="1.0",
        app_version="1.0",
        lang_code="ru",
    )
    telethon_clients[phone] = client
    return client


async def telethon_send_code(phone: str) -> str:
    """Шлёт код. Возвращает phone_code_hash (Telethon хранит его сам)."""
    client = get_telethon_client(phone)
    await client.connect()
    sent = await client.send_code_request(phone)
    return sent.phone_code_hash


async def telethon_sign_in(phone: str, code: str) -> tuple[bool, str]:
    """Логинится по коду. Возвращает (успех, session_string)."""
    client = telethon_clients.get(phone)
    if not client:
        return False, ""
    try:
        await client.sign_in(phone, code)
        session_str = client.session.save()
        return True, session_str
    except SessionPasswordNeededError:
        log.warning("2FA required for %s", phone)
        return False, ""
    except PhoneCodeInvalidError:
        return False, ""
    except Exception as e:
        log.exception("sign_in error")
        return False, ""


async def telethon_logout(phone: str) -> None:
    """Выходит с аккаунта и удаляет клиент из пула."""
    client = telethon_clients.pop(phone, None)
    if not client:
        return
    try:
        if client.is_connected():
            await client.log_out()
    except Exception:
        pass
    try:
        await client.disconnect()
    except Exception:
        pass


def make_code_handler(phone: str, target_user_id: int):
    """Обработчик входящего сообщения — ловит код и шлёт покупателю."""
    async def handler(event):
        try:
            msg = event.message
            if not msg or not msg.message:
                return
            text = msg.message.strip()
            if not text:
                return
            log.info("Telethon [%s] caught: %s", phone, text[:30])
            try:
                await bot.send_message(
                    target_user_id,
                    f"🔐 <b>Код для аккаунта <code>{phone}</code>:</b>\n\n"
                    f"<code>{text}</code>\n\n"
                    f"Введите его в Telegram. После этого бот автоматически выйдет."
                )
            except Exception as e:
                log.exception("send code to buyer: %s", e)
        except Exception as e:
            log.exception("code handler: %s", e)
    return handler


def register_code_listener(phone: str, buyer_user_id: int):
    """Подписывает Telethon-клиент на входящие коды для конкретного покупателя."""
    client = telethon_clients.get(phone)
    if not client:
        return
    telethon_waiting[phone] = buyer_user_id
    user_waiting_for[buyer_user_id] = phone
    handler = make_code_handler(phone, buyer_user_id)
    client.add_event_handler(handler, events.NewMessage(incoming=True))


# ══════════════════════════════════════════════
#  OCR
# ══════════════════════════════════════════════
RECIPIENT_KEYWORDS = ["максим", "сергеевич"]
AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d{1,2})?|\d+)(?:\s?₽|\s?руб|RUB|р\.)?",
    re.IGNORECASE,
)


def _normalize_amount(s: str) -> Optional[int]:
    s = s.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _ocr_sync(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("L")
    img = img.resize((img.width * 2, img.height * 2))
    return pytesseract.image_to_string(img, lang="rus+eng")


async def recognize_screenshot(image_bytes: bytes) -> dict:
    try:
        text = await asyncio.to_thread(_ocr_sync, image_bytes)
    except Exception as e:
        log.exception("OCR error")
        return {"ok": False, "error": str(e)}

    text_l = text.lower()
    name_correct = any(kw in text_l for kw in RECIPIENT_KEYWORDS)

    candidates = []
    for m in AMOUNT_RE.finditer(text):
        amt = _normalize_amount(m.group(1))
        if amt and 10 <= amt <= 1_000_000:
            candidates.append(amt)
    detected_amount = max(candidates) if candidates else None

    return {
        "ok": True,
        "name_correct": name_correct,
        "detected_amount": detected_amount,
        "raw_text": text[:400],
    }


def check_payment(ocr_result: dict, expected_price: int) -> dict:
    if not ocr_result.get("ok"):
        return {"valid": False, "reason": "ocr_error"}
    if not ocr_result["name_correct"]:
        return {"valid": False, "reason": "wrong_name"}
    detected = ocr_result["detected_amount"]
    if detected is None:
        return {"valid": False, "reason": "no_amount"}
    if detected != expected_price:
        return {
            "valid": False,
            "reason": "wrong_amount",
            "detected": detected,
            "expected": expected_price,
        }
    return {"valid": True, "detected_amount": detected}


# ══════════════════════════════════════════════
#  ТЕКСТЫ
# ══════════════════════════════════════════════
TOS_TEXT = (
    "📜 <b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b>\n\n"
    "Используя данный бот, вы подтверждаете, что:\n\n"
    "1. <b>Возраст.</b> Вам исполнилось 18 лет.\n\n"
    "2. <b>Назначение.</b> Аккаунты Telegram продаются как цифровой товар. "
    "Вы понимаете, что покупаете <i>номер телефона</i>, к которому привязан аккаунт, "
    "и обязуетесь использовать его в рамках правил Telegram.\n\n"
    "3. <b>Возврат.</b> После передачи данных аккаунта возврат средств "
    "<b>невозможен</b>.\n\n"
    "4. <b>Блокировки.</b> Продавец не несёт ответственности за:\n"
    "   • блокировку аккаунта Telegram за нарушение ToS;\n"
    "   • спам, жалобы со стороны третьих лиц;\n"
    "   • действия нового владельца после получения доступа.\n\n"
    "5. <b>Оплата.</b> Способы: перевод по СБП (Сбер) и Telegram Stars.\n\n"
    "6. <b>Конфиденциальность.</b> Бот хранит ваш Telegram ID, имя и количество покупок. "
    "Скриншоты оплаты обрабатываются только для подтверждения перевода.\n\n"
    "7. <b>Запрещено:</b>\n"
    "   • использовать аккаунт для мошенничества и спама;\n"
    "   • перепродавать аккаунт дороже купленной цены;\n"
    "   • запрашивать возврат после получения данных.\n\n"
    "Нажимая «✅ Принимаю», вы подтверждаете, что прочитали и принимаете все условия."
)

WELCOME = (
    "👋 <b>Здравствуйте!</b>\n\n"
    "Здесь вы сможете приобрести физические аккаунты Telegram "
    "за несколько минут — безопасно и качественно.\n\n"
    "Выберите действие:"
)

WELCOME_DECLINED = (
    "❌ Вы отказались от пользовательского соглашения.\n"
    "Бот недоступен. Чтобы вернуться — нажмите /start и примите условия."
)

PROFILE_TPL = (
    "👤 <b>Ваш профиль</b>\n\n"
    "📛 Ник: <b>{nick}</b>\n"
    "🆔 #TG-ID: <code>{tid}</code>\n"
    "⏱ С нами: {since}\n"
    "🛒 Куплено аккаунтов: <b>{purchases}</b>\n"
    "💸 Потрачено: <b>{rub} ₽</b> / <b>{stars} ⭐</b>"
)

SUPPORT_TEXT = (
    "🛎 <b>Тех-поддержка</b>\n\n"
    "Это человек, к которому вы можете обратиться, если:\n"
    "• нашли баг;\n"
    "• вам не выдали аккаунт;\n"
    "• возникли другие проблемы.\n\n"
    "Опишите всё максимально подробно и спокойно. "
    "Если ответа нет долгое время — напишите ещё раз."
)

CATALOG_INTRO = (
    "🛒 <b>Каталог аккаунтов</b>\n\n"
    "Вы попали в каталог всех аккаунтов. "
    "Выберите подходящий и покупайте."
)

NO_ACCOUNTS = "😔 В каталоге пока ничего нет. Загляните позже!"

BUY_CONFIRM_TPL = (
    "Вы выбрали аккаунт — {flag} <b>{country} {year}</b> года регистрации.\n"
    "Цена: <b>{price} ₽</b> (~ <b>{stars} ⭐</b>).\n\n"
    "Вы уверены, что хотите его приобрести?"
)

INSTRUCTION_TEXT = (
    "📖 <b>Инструкция по добавлению аккаунта:</b>\n\n"
    "1. Зайдите в <b>Настройки</b> Telegram\n"
    "2. Зайдите в раздел <b>«Аккаунт»</b> и пролистайте в самый низ\n"
    "3. Нажмите <b>«Добавить аккаунт»</b>\n"
    "4. Введите номер телефона, который бот отправит после «Далее»\n"
    "5. Бот вышлет код — введите его в специальное поле\n"
    "6. Аккаунт полностью ваш ✅"
)

PHONE_TPL = (
    "📞 <b>Номер телефона</b>, который нужно вписать в поле "
    "после нажатия <b>«Добавить аккаунт»</b>:\n\n"
    "<code>{phone}</code>\n\n"
    "После того, как Telegram отправит код — пришлите его сюда."
)

THANKS_TEXT = (
    "🙏 <b>Спасибо за покупку в нашем боте!</b>\n\n"
    "Рады, что вы нам доверяете.\n\n"
    "📌 <b>Рекомендации по аккаунту:</b>\n"
    "1. Не трогайте аккаунт в течение 20–40 часов, особенно если он из США.\n"
    "2. Установите облачный пароль и код-пароль.\n"
    "3. Привяжите свою почту (Email).\n\n"
    "❄️ <b>Что делать, если аккаунт всё-таки заморозили?</b>\n"
    "Напишите в Тех-поддержку — постараемся помочь.\n\n"
    "Удачи и ждём дальнейших покупок! 🎉"
)

SBER_TPL = (
    "💳 <b>Реквизиты для перевода по СБП:</b>\n\n"
    "<code>{card}</code> — <b>{recipient}</b>\n"
    "🏦 {bank}\n\n"
    "💰 Сумма: <b>{price} ₽</b>\n\n"
    "⚠️ <b>Важно:</b>\n"
    "• Сумма должна совпадать <b>точно</b>;\n"
    "• На скриншоте должно быть видно имя «Максим».\n\n"
    "📤 После оплаты пришлите сюда скриншот перевода."
)


# ══════════════════════════════════════════════
#  КНОПКИ
# ══════════════════════════════════════════════
def kb_tos() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю", callback_data="tos_accept")],
        [InlineKeyboardButton(text="❌ Отказаться", callback_data="tos_decline")],
    ])


def kb_main(is_adm: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="catalog")],
        [InlineKeyboardButton(text="🛎 Тех-поддержка", callback_data="support")],
    ]
    if is_adm:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")],
    ])


def kb_support() -> InlineKeyboardMarkup:
    url = f"https://t.me/{SUPPORT_USERNAME}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать Тех-поддержке", url=url)],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")],
    ])


def kb_catalog_list(accounts: list) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        stars = rub_to_stars(a.price)
        label = f"{a.flag} {a.country} | {a.year} | {a.age} лет — {a.price}₽ ({stars}⭐)"
        if len(label) > 64:
            label = f"{a.flag} {a.country} | {a.year} | {a.price}₽"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"pick|{a.id}")])
    rows.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_buy_confirm(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"buy|{acc_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="catalog"),
        ],
    ])


def kb_payment(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 СБП", callback_data=f"pay_sber|{acc_id}")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"pay_stars|{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="catalog")],
    ])


def kb_instruction_next() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Далее", callback_data="next_phone")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="adm_add_acc")],
        [InlineKeyboardButton(text="✏️ Изменить аккаунт", callback_data="adm_edit_list")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data="adm_delete_list")],
        [InlineKeyboardButton(text="👑 Добавить админа", callback_data="adm_add_admin")],
        [InlineKeyboardButton(text="🚫 Снять админа", callback_data="adm_remove_admin")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
    ])


# ══════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════
class Tos(StatesGroup):
    waiting = State()


class Buy(StatesGroup):
    wait_screenshot = State()
    wait_code = State()


class AdminFSM(StatesGroup):
    menu = State()
    add_country = State()
    add_year = State()
    add_age = State()
    add_price = State()
    add_phone = State()
    waiting_code = State()      # ждём код от админа для нового аккаунта
    edit_field = State()
    edit_value = State()
    add_admin_id = State()
    remove_admin_id = State()


# ══════════════════════════════════════════════
#  ХЕНДЛЕРЫ — /start + Соглашение
# ══════════════════════════════════════════════
@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        u = await get_or_create_user(s, msg.from_user)
    if not u.tos_accepted:
        await msg.answer(TOS_TEXT, reply_markup=kb_tos())
        await state.set_state(Tos.waiting)
        return
    is_adm = await is_admin(msg.from_user.id)
    await msg.answer(WELCOME, reply_markup=kb_main(is_adm))


@router.callback_query(F.data == "tos_accept")
async def tos_accept(cb: CallbackQuery, state: FSMContext):
    async with SessionLocal() as s:
        u = await get_or_create_user(s, cb.from_user)
        u.tos_accepted = True
        await s.commit()
    await state.clear()
    is_adm = await is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME, reply_markup=kb_main(is_adm))
    await cb.answer()


@router.callback_query(F.data == "tos_decline")
async def tos_decline(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(WELCOME_DECLINED)
    await state.clear()
    await cb.answer()


@router.callback_query(F.data == "back_start")
async def back_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    is_adm = await is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME, reply_markup=kb_main(is_adm))
    await cb.answer()


# ══════════════════════════════════════════════
#  ПРОФИЛЬ
# ══════════════════════════════════════════════
def _fmt_since(dt: datetime) -> str:
    diff = datetime.utcnow() - dt
    secs = int(diff.total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    async with SessionLocal() as s:
        u = await get_or_create_user(s, cb.from_user)
        await s.refresh(u)
        text = PROFILE_TPL.format(
            nick=u.full_name or "—",
            tid=u.id,
            since=_fmt_since(u.created_at),
            purchases=u.purchases,
            rub=u.spent_rub,
            stars=u.spent_stars,
        )
    await cb.message.edit_text(text, reply_markup=kb_back_to_main())
    await cb.answer()


# ══════════════════════════════════════════════
#  ТЕХ-ПОДДЕРЖКА
# ══════════════════════════════════════════════
@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    await cb.message.edit_text(SUPPORT_TEXT, reply_markup=kb_support())
    await cb.answer()


# ══════════════════════════════════════════════
#  КАТАЛОГ
# ══════════════════════════════════════════════
@router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    accounts = await all_accounts()
    if not accounts:
        await cb.message.edit_text(NO_ACCOUNTS, reply_markup=kb_back_to_main())
        await cb.answer()
        return
    await cb.message.edit_text(
        CATALOG_INTRO,
        reply_markup=kb_catalog_list(accounts),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("pick|"))
async def cb_pick(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if not acc:
        await cb.answer("😔 Аккаунт уже продан.", show_alert=True)
        await cb.message.edit_text(NO_ACCOUNTS, reply_markup=kb_back_to_main())
        return
    await state.update_data(account_id=acc_id)
    await cb.message.edit_text(
        BUY_CONFIRM_TPL.format(
            flag=acc.flag,
            country=acc.country,
            year=acc.year,
            price=acc.price,
            stars=rub_to_stars(acc.price),
        ),
        reply_markup=kb_buy_confirm(acc_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("buy|"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if not acc:
        await cb.answer("😔 Аккаунт уже продан.", show_alert=True)
        return
    await state.update_data(account_id=acc_id)
    await cb.message.edit_text(
        "💳 <b>Выберите способ оплаты:</b>",
        reply_markup=kb_payment(acc_id),
    )
    await cb.answer()
  # ══════════════════════════════════════════════
#  ОПЛАТА: STARS
# ══════════════════════════════════════════════
@router.callback_query(F.data.startswith("pay_stars|"))
async def cb_pay_stars(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if not acc:
        await cb.answer("😔 Аккаунт уже продан.", show_alert=True)
        return
    stars = rub_to_stars(acc.price)
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"Аккаунт {acc.country} {acc.year}",
            description=f"Физический аккаунт Telegram — {acc.country}, {acc.year} г.",
            payload=f"acc_{acc_id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Аккаунт", amount=stars)],
        )
        await cb.answer()
    except Exception as e:
        log.exception("send_invoice")
        await cb.message.edit_text("❌ Ошибка при создании счёта.", reply_markup=kb_back_to_main())


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_stars_paid(msg: Message, state: FSMContext):
    payload = msg.successful_payment.invoice_payload or ""
    if not payload.startswith("acc_"):
        return
    acc_id = int(payload[4:])
    acc = await get_account(acc_id)
    if not acc:
        await msg.answer("⚠️ Аккаунт уже недоступен. Напишите в поддержку.")
        return

    await state.update_data(
        account_id=acc_id, phone=acc.phone, country=acc.country,
        year=acc.year, price=acc.price, pay_method="stars",
    )
    await msg.answer(
        f"✅ <b>Вы успешно оплатили физический аккаунт {acc.flag} {acc.country} {acc.year} г.</b>\n\n"
        f"{INSTRUCTION_TEXT}",
        reply_markup=kb_instruction_next(),
    )
    await state.set_state(Buy.wait_code)


# ══════════════════════════════════════════════
#  ОПЛАТА: СБП (скрин)
# ══════════════════════════════════════════════
@router.callback_query(F.data.startswith("pay_sber|"))
async def cb_pay_sber(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if not acc:
        await cb.answer("😔 Аккаунт уже продан.", show_alert=True)
        return
    await state.update_data(
        account_id=acc_id, phone=acc.phone, country=acc.country,
        year=acc.year, price=acc.price,
    )
    await cb.message.edit_text(
        SBER_TPL.format(card=SBER_CARD, recipient=SBER_RECIPIENT, bank=SBER_BANK, price=acc.price),
        reply_markup=kb_back_to_main(),
    )
    await state.set_state(Buy.wait_screenshot)
    await cb.answer()


@router.message(StateFilter(Buy.wait_screenshot), F.photo)
async def on_screenshot(msg: Message, state: FSMContext):
    data = await state.get_data()
    expected = int(data.get("price", 0))
    acc_id = data.get("account_id")
    if not acc_id:
        await msg.answer("⚠️ Сессия истекла. /start")
        await state.clear()
        return

    photo = msg.photo[-1]
    f = await bot.download(photo.file_id, destination=None)
    image_bytes = f.read()

    wait = await msg.answer("🔍 Проверяю скриншот…")
    ocr_res = await recognize_screenshot(image_bytes)
    verdict = check_payment(ocr_res, expected)
    log.info("OCR: %s", verdict)
    await wait.delete()

    if not verdict["valid"]:
        reason = verdict["reason"]
        if reason == "wrong_name":
            await msg.answer(
                f"❌ <b>Неверный получатель</b>\n\n"
                f"Должно быть: <b>{SBER_RECIPIENT}</b>.\n"
                f"Пришлите новый скриншот.",
                reply_markup=kb_back_to_main(),
            )
        elif reason == "wrong_amount":
            await msg.answer(
                f"❌ <b>Неверная сумма</b>\n\n"
                f"На скриншоте: <b>{verdict['detected']} ₽</b>\n"
                f"Ожидается: <b>{expected} ₽</b>",
                reply_markup=kb_back_to_main(),
            )
        elif reason == "no_amount":
            await msg.answer(
                "❌ Не удалось распознать сумму. Пришлите более чёткий скриншот.",
                reply_markup=kb_back_to_main(),
            )
        else:
            await msg.answer(
                "❌ Не удалось обработать скриншот. Попробуйте ещё раз.",
                reply_markup=kb_back_to_main(),
            )
        return

    acc = await get_account(acc_id)
    if not acc:
        await msg.answer("😔 Аккаунт уже продан. Напишите в поддержку.")
        return
    await state.update_data(pay_method="sber")
    await msg.answer(
        f"✅ <b>Оплата подтверждена!</b>\n\n"
        f"Аккаунт {acc.flag} {acc.country} {acc.year} г.\n\n"
        f"{INSTRUCTION_TEXT}",
        reply_markup=kb_instruction_next(),
    )
    await state.set_state(Buy.wait_code)


@router.message(StateFilter(Buy.wait_screenshot))
async def on_screenshot_bad(msg: Message):
    await msg.answer("📤 Пришлите именно <b>скриншот</b> (фото), не текст.")


# ══════════════════════════════════════════════
#  ВЫДАЧА АККАУНТА: кнопка «Далее» + Telethon
# ══════════════════════════════════════════════
@router.callback_query(F.data == "next_phone", StateFilter(Buy.wait_code))
async def cb_next_phone(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone", "—")
    await cb.message.edit_text(PHONE_TPL.format(phone=phone))
    await cb.answer()


@router.message(StateFilter(Buy.wait_code))
async def on_code(msg: Message, state: FSMContext):
    """
    Сценарий выдачи:
    1) Telethon-клиент на этом аккаунте уже активен (запущен в main()).
    2) Регистрируем слушатель входящих сообщений.
    3) Когда покупатель введёт номер у себя — Telegram пришлёт код
       в Saved Messages этому аккаунту.
    4) Telethon ловит → бот шлёт код покупателю.
    5) Через 5 минут бот выходит с аккаунта (log_out).
    """
    data = await state.get_data()
    phone = data.get("phone")
    acc_id = data.get("account_id")
    pay_method = data.get("pay_method", "sber")

    if not phone or not acc_id:
        await msg.answer("⚠️ Сессия истекла. /start")
        await state.clear()
        return

    acc = await get_account(acc_id)
    if not acc:
        await msg.answer("😔 Аккаунт уже продан.")
        return

    # 1) Проверяем, что Telethon-клиент живой
    client = telethon_clients.get(phone)
    if not client or not client.is_connected():
        await msg.answer("⏳ Готовлю аккаунт к выдаче…")
        try:
            client = get_telethon_client(phone, acc.session_str)
            await client.connect()
        except Exception as e:
            log.exception("telethon connect")
            await msg.answer("⚠️ Ошибка подключения к аккаунту. Напишите в поддержку.")
            return

    if client and not await client.is_user_authorized():
        await msg.answer(
            "⚠️ Аккаунт не залогинен в фоне. "
            "Обратитесь к администратору для переавторизации."
        )
        return

    # 2) Регистрируем слушатель
    register_code_listener(phone, msg.from_user.id)

    # 3) Фиксируем покупку + удаляем из каталога
    await record_purchase(msg.from_user.id, acc, pay_method)
    await delete_account(acc_id)

    # 4) Сообщение покупателю
    await msg.answer(
        f"✅ <b>Аккаунт {acc.flag} {acc.country} {acc.year} г. зарезервирован за вами.</b>\n\n"
        f"📞 Номер: <code>{phone}</code>\n\n"
        f"📨 Войдите в этот аккаунт через:\n"
        f"<b>Настройки → Аккаунт → Добавить аккаунт → {phone}</b>\n\n"
        f"🔐 Как только Telegram пришлёт код — я перешлю его вам сюда.\n"
        f"После ввода кода аккаунт ваш, бот автоматически выйдет."
    )

    # 5) Планируем выход через 5 минут
    asyncio.create_task(_post_purchase_cleanup(phone, msg.from_user.id))
    await state.clear()


async def _post_purchase_cleanup(phone: str, buyer_id: int, wait_minutes: int = 5):
    """Через N минут — выходим с аккаунта."""
    try:
        await asyncio.sleep(wait_minutes * 60)
        if telethon_waiting.get(phone) == buyer_id:
            log.info("Cleanup: logging out from %s", phone)
            try:
                await bot.send_message(
                    buyer_id,
                    f"🚪 <b>Бот вышел с аккаунта <code>{phone}</code>.</b>\n\n"
                    f"Аккаунт полностью ваш. Спасибо за покупку!\n\n{THANKS_TEXT}"
                )
            except Exception:
                pass
            telethon_waiting.pop(phone, None)
            user_waiting_for.pop(buyer_id, None)
            await telethon_logout(phone)
    except Exception as e:
        log.exception("cleanup: %s", e)


# ══════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ
# ══════════════════════════════════════════════
def _main_admin_only(uid: int) -> bool:
    return uid == ADMIN_ID


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminFSM.menu)
    await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=kb_admin())
    await cb.answer()


# ─── Статистика ───
@router.callback_query(F.data == "adm_stats", StateFilter(AdminFSM.menu))
async def cb_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    st = await stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{st['users']}</b>\n"
        f"🛒 Куплено аккаунтов: <b>{st['sold']}</b>\n"
        f"📦 В каталоге: <b>{st['available']}</b>\n"
        f"📚 За всё время: <b>{st['total']}</b>\n"
        f"💰 Потрачено: <b>{st['money_rub']} ₽</b>\n"
        f"⭐ Потрачено: <b>{st['money_stars']} ⭐</b>"
    )
    await cb.message.edit_text(text, reply_markup=kb_admin())
    await cb.answer()


# ─── Добавить аккаунт (с авторизацией по коду) ───
COUNTRY_FLAGS = {
    "россия": "🇷🇺", "рф": "🇷🇺", "russia": "🇷🇺",
    "сша": "🇺🇸", "usa": "🇺🇸", "united states": "🇺🇸", "америка": "🇺🇸",
    "uk": "🇬🇧", "великобритания": "🇬🇧", "англия": "🇬🇧",
    "германия": "🇩🇪", "germany": "🇩🇪",
    "италия": "🇮🇹", "italy": "🇮🇹",
    "франция": "🇫🇷", "france": "🇫🇷",
    "норвегия": "🇳🇴", "norway": "🇳🇴",
    "казахстан": "🇰🇿", "kazakhstan": "🇰🇿",
    "украина": "🇺🇦", "ukraine": "🇺🇦",
    "беларусь": "🇧🇾", "belarus": "🇧🇾",
    "польша": "🇵🇱", "poland": "🇵🇱",
    "чехия": "🇨🇿", "czech": "🇨🇿",
    "испания": "🇪🇸", "spain": "🇪🇸",
}


def _flag_for(country: str) -> str:
    return COUNTRY_FLAGS.get(country.lower().strip(), "🌍")


@router.callback_query(F.data == "adm_add_acc", StateFilter(AdminFSM.menu))
async def adm_add_acc(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return
    await state.set_state(AdminFSM.add_country)
    await cb.message.edit_text(
        "➕ <b>Добавление аккаунта</b>\n\n"
        "<b>Шаг 1/6</b> — Введите страну:",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(StateFilter(AdminFSM.add_country))
async def adm_enter_country(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    country = (msg.text or "").strip()
    if not country:
        await msg.answer("❌ Введите страну текстом.")
        return
    await state.update_data(country=country)
    await state.set_state(AdminFSM.add_year)
    await msg.answer(
        "<b>Шаг 2/6</b> — Введите год регистрации:\n"
        f"<i>От 2010 до {datetime.now().year}</i>",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AdminFSM.add_year))
async def adm_enter_year(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    try:
        year = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите год числом, например 2020")
        return
    if not (2010 <= year <= datetime.now().year):
        await msg.answer(f"❌ Год должен быть от 2010 до {datetime.now().year}")
        return
    await state.update_data(year=year)
    await state.set_state(AdminFSM.add_age)
    await msg.answer(
        "<b>Шаг 3/6</b> — Введите отлёгу (сколько лет):\n"
        "<i>Например: 6</i>",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AdminFSM.add_age))
async def adm_enter_age(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    try:
        age = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите число лет, например 6")
        return
    if age < 0 or age > 30:
        await msg.answer("❌ Отлёга должна быть от 0 до 30")
        return
    await state.update_data(age=age)
    await state.set_state(AdminFSM.add_price)
    await msg.answer(
        "<b>Шаг 4/6</b> — Введите цену в рублях:\n"
        "<i>Например: 400</i>",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AdminFSM.add_price))
async def adm_enter_price(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    try:
        price = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите цену числом")
        return
    if price <= 0:
        await msg.answer("❌ Цена должна быть > 0")
        return
    await state.update_data(price=price)
    await state.set_state(AdminFSM.add_phone)
    await msg.answer(
        "<b>Шаг 5/6</b> — Введите номер телефона аккаунта:\n"
        "<i>Формат: +11112223344 (или 11112223344)</i>",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AdminFSM.add_phone))
async def adm_enter_phone(msg: Message, state: FSMContext):
    """Шаг 5 — ввод номера. Шлём код через Telethon."""
    if not await is_admin(msg.from_user.id):
        return
    phone = (msg.text or "").strip().replace(" ", "").replace("-", "")
    if not phone.lstrip("+").isdigit() or len(phone.lstrip("+")) < 7:
        await msg.answer("❌ Это не номер. Попробуйте ещё раз.")
        return

    # Шлём код через Telethon
    try:
        client = get_telethon_client(phone)
        await client.connect()
        await client.send_code_request(phone)
    except Exception as e:
        log.exception("send_code_request")
        await msg.answer(f"❌ Ошибка Telethon: {e}")
        return

    await state.update_data(phone=phone)
    await state.set_state(AdminFSM.waiting_code)
    await msg.answer(
        "📨 <b>Шаг 6/6</b> — Код выслан в Telegram этому аккаунту.\n\n"
        "Откройте Telegram на аккаунте (Saved Messages / сообщение от Telegram), "
        "скопируйте код и пришлите сюда.\n\n"
        "<i>Код выглядит как 5 цифр, например: 12345</i>",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AdminFSM.waiting_code))
async def adm_enter_code(msg: Message, state: FSMContext):
    """Шаг 6 — ввод кода от Telegram. Логинимся, сохраняем сессию, добавляем в БД."""
    if not await is_admin(msg.from_user.id):
        return
    code = (msg.text or "").strip()
    if not re.fullmatch(r"\d{4,6}", code):
        await msg.answer("❌ Код должен быть 4–6 цифр.")
        return

    data = await state.get_data()
    phone = data["phone"]

    ok, session_str = await telethon_sign_in(phone, code)
    if not ok:
        await msg.answer(
            "❌ Не удалось войти. Возможно, код неверный или включена 2FA.\n"
            "Попробуйте ещё раз или отмените."
        )
        return

    # Сохраняем сессию в БД
    flag = _flag_for(data["country"])
    acc = await add_account(
        phone=phone,
        country=data["country"],
        flag=flag,
        year=data["year"],
        age=data["age"],
        price=data["price"],
        session_str=session_str,
    )
    if not acc:
        await telethon_logout(phone)
        await state.set_state(AdminFSM.menu)
        await msg.answer(f"❌ Номер <code>{phone}</code> уже в базе.", reply_markup=kb_admin())
        return

    await state.set_state(AdminFSM.menu)
    await msg.answer(
        f"✅ <b>Аккаунт добавлен и залогинен в фоне</b>\n\n"
        f"{flag} {data['country']} | {data['year']} | {data['age']} лет\n"
        f"📞 <code>{phone}</code>\n"
        f"💰 {data['price']} ₽ (~ {rub_to_stars(data['price'])} ⭐)",
        reply_markup=kb_admin(),
    )


# ─── Редактировать аккаунт ───
@router.callback_query(F.data == "adm_edit_list", StateFilter(AdminFSM.menu))
async def adm_edit_list(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    accounts = await all_accounts()
    if not accounts:
        await cb.message.edit_text("Каталог пуст.", reply_markup=kb_admin())
        await cb.answer()
        return
    rows = []
    for a in accounts:
        label = f"{a.flag} {a.country} | {a.year} | {a.price}₽"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_edit|{a.id}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text(
        "✏️ <b>Выберите аккаунт для редактирования:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_edit|"), StateFilter(AdminFSM.menu))
async def adm_edit_pick(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return
    acc_id = int(cb.data.split("|", 1)[1])
    await state.update_data(edit_id=acc_id)
    rows = [
        [InlineKeyboardButton(text="🌍 Страна", callback_data="adm_field|country")],
        [InlineKeyboardButton(text="📅 Год", callback_data="adm_field|year")],
        [InlineKeyboardButton(text="⏳ Отлёга", callback_data="adm_field|age")],
        [InlineKeyboardButton(text="💰 Цена", callback_data="adm_field|price")],
        [InlineKeyboardButton(text="📞 Номер", callback_data="adm_field|phone")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_edit_list")],
    ]
    await cb.message.edit_text("Что меняем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("adm_field|"), StateFilter(AdminFSM.menu))
async def adm_field(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return
    field = cb.data.split("|", 1)[1]
    await state.update_data(edit_field=field)
    await state.set_state(AdminFSM.edit_value)
    titles = {
        "country": "🌍 новую страну",
        "year": "📅 новый год (число)",
        "age": "⏳ новую отлёгу (число)",
        "price": "💰 новую цену (число)",
        "phone": "📞 новый номер (только если ещё не залогинен)",
    }
    await cb.message.edit_text(f"Введите {titles[field]}:", reply_markup=kb_cancel())
    await cb.answer()


@router.message(StateFilter(AdminFSM.edit_value))
async def adm_field_value(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    data = await state.get_data()
    acc_id = data.get("edit_id")
    field = data.get("edit_field")
    value = (msg.text or "").strip()

    cast = value
    if field in ("year", "age", "price"):
        try:
            cast = int(value)
        except ValueError:
            await msg.answer("❌ Введите число.")
            return
    elif field == "phone":
        cast = value.replace(" ", "").replace("-", "")
        if not cast.lstrip("+").isdigit():
            await msg.answer("❌ Это не номер.")
            return
    elif field == "country":
        cast = value

    kwargs = {field: cast}
    if field == "country":
        kwargs["flag"] = _flag_for(value)
    ok = await update_account(acc_id, **kwargs)
    await state.set_state(AdminFSM.menu)
    if ok:
        await msg.answer("✅ <b>Изменено.</b>", reply_markup=kb_admin())
    else:
        await msg.answer("❌ Аккаунт не найден.", reply_markup=kb_admin())


# ─── Удалить аккаунт ───
@router.callback_query(F.data == "adm_delete_list", StateFilter(AdminFSM.menu))
async def adm_delete_list(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    accounts = await all_accounts()
    if not accounts:
        await cb.message.edit_text("Каталог пуст.", reply_markup=kb_admin())
        await cb.answer()
        return
    rows = []
    for a in accounts:
        label = f"🗑 {a.flag} {a.country} | {a.year} | {a.price}₽"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_del|{a.id}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text(
        "🗑 <b>Выберите аккаунт для удаления:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_del|"), StateFilter(AdminFSM.menu))
async def adm_del_confirm(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if not acc:
        await cb.answer("Уже удалён", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_del_yes|{acc_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="adm_delete_list"),
        ],
    ]
    await cb.message.edit_text(
        f"Удалить {acc.flag} <b>{acc.country} {acc.year}</b> ({acc.price}₽)?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_del_yes|"), StateFilter(AdminFSM.menu))
async def adm_del_yes(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    acc_id = int(cb.data.split("|", 1)[1])
    acc = await get_account(acc_id)
    if acc:
        await telethon_logout(acc.phone)
    await delete_account(acc_id)
    await cb.message.edit_text("✅ Аккаунт удалён.", reply_markup=kb_admin())
    await cb.answer()


# ─── Управление админами (только главный) ───
@router.callback_query(F.data == "adm_add_admin", StateFilter(AdminFSM.menu))
async def adm_add_admin(cb: CallbackQuery, state: FSMContext):
    if not _main_admin_only(cb.from_user.id):
        await cb.answer("⛔ Только для главного админа", show_alert=True)
        return
    await state.set_state(AdminFSM.add_admin_id)
    await cb.message.edit_text(
        "👑 Введите Telegram ID нового админа:",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(StateFilter(AdminFSM.add_admin_id))
async def adm_add_admin_done(msg: Message, state: FSMContext):
    if not _main_admin_only(msg.from_user.id):
        return
    try:
        new_id = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите ID числом")
        return
    async with SessionLocal() as s:
        u = await s.get(User, new_id)
        uname = u.username if u else ""
    await add_admin(new_id, uname)
    await state.set_state(AdminFSM.menu)
    await msg.answer(f"✅ Пользователь <code>{new_id}</code> назначен админом.", reply_markup=kb_admin())


@router.callback_query(F.data == "adm_remove_admin", StateFilter(AdminFSM.menu))
async def adm_remove_admin(cb: CallbackQuery):
    if not _main_admin_only(cb.from_user.id):
        await cb.answer("⛔ Только для главного админа", show_alert=True)
        return
    admins = await list_admins()
    rows = []
    for a in admins:
        if a.id == ADMIN_ID:
            continue
        rows.append([InlineKeyboardButton(
            text=f"❌ {a.id} (@{a.username or '—'})",
            callback_data=f"adm_rm|{a.id}",
        )])
    if not rows:
        await cb.message.edit_text("Дополнительных админов нет.", reply_markup=kb_admin())
        await cb.answer()
        return
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text(
        "Выберите админа для снятия:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_rm|"))
async def adm_rm_confirm(cb: CallbackQuery):
    if not _main_admin_only(cb.from_user.id):
        return
    rm_id = int(cb.data.split("|", 1)[1])
    await cb.message.edit_text(
        f"Снять <code>{rm_id}</code> с админки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"adm_rm_yes|{rm_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="adm_remove_admin"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm_rm_yes|"))
async def adm_rm_yes(cb: CallbackQuery):
    if not _main_admin_only(cb.from_user.id):
        return
    rm_id = int(cb.data.split("|", 1)[1])
    await remove_admin(rm_id)
    await cb.message.edit_text(f"✅ <code>{rm_id}</code> снят с админки.", reply_markup=kb_admin())
    await cb.answer()


# ══════════════════════════════════════════════
#  FALLBACK
# ══════════════════════════════════════════════
@router.message()
async def fallback(msg: Message):
    if msg.text and not msg.text.startswith("/"):
        is_adm = await is_admin(msg.from_user.id)
        await msg.answer(WELCOME, reply_markup=kb_main(is_adm))


# ══════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════
async def main():
    await init_db()
    log.info("Bot starting…")

    # Поднимаем Telethon-клиенты для всех аккаунтов в каталоге
    accounts = await all_accounts()
    for a in accounts:
        if not a.session_str:
            log.warning("Account %s has no session_str, skip", a.phone)
            continue
        try:
            client = get_telethon_client(a.phone, a.session_str)
            await client.connect()
            if await client.is_user_authorized():
                log.info("Telethon client for %s is live", a.phone)
            else:
                log.warning("Telethon client for %s NOT authorized", a.phone)
        except Exception as e:
            log.exception("Telethon boot for %s: %s", a.phone, e)

    log.info("All clients ready")
    try:
        await dp.start_polling(bot)
    finally:
        # Закрываем все Telethon-клиенты корректно
        for phone in list(telethon_clients.keys()):
            try:
                await telethon_clients[phone].disconnect()
            except Exception:
                pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
