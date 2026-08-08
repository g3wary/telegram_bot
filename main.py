"""
Telegram-магазин физических аккаунтов.
aiogram 3 + SQLAlchemy 2 (async) + Tesseract OCR.

Сценарий покупки:
  1) /start → Соглашение
  2) Каталог: страна → год
  3) Оплата: Сбер (скрин) или Stars
  4) Бот проверяет → выдаёт телефон
  5) Пользователь шлёт "код" → бот имитирует приём → "Аккаунт активирован"
"""
import logging
import asyncio
import re

from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, ADMIN_ID, SBER_CARD, SBER_RECIPIENT
import database as db
from states import Tos, Buy, AdminFSM
from keyboards import (
    kb_start, kb_tos, kb_profile, kb_countries, kb_years,
    kb_payment, kb_back_to_catalog, kb_admin, kb_admins_manage, kb_cancel_admin,
)
from text import WELCOME, TOS_TEXT
import ocr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("shop")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ════════════════════════════════════════════════════════════════
#  /start + Соглашение
# ════════════════════════════════════════════════════════════════
@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    async with db.SessionLocal() as s:
        user = await db.get_or_create_user(s, msg.from_user)
    if not user.tos_accepted:
        await msg.answer(TOS_TEXT, reply_markup=kb_tos())
        await state.set_state(Tos.waiting)
        return
    is_adm = await db.is_admin(msg.from_user.id)
    await msg.answer(WELCOME, reply_markup=kb_start(is_adm))


@router.callback_query(F.data == "tos_accept")
async def tos_accept(cb: CallbackQuery, state: FSMContext):
    async with db.SessionLocal() as s:
        u = await db.get_or_create_user(s, cb.from_user)
        u.tos_accepted = True
        await s.commit()
    await state.clear()
    is_adm = await db.is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME, reply_markup=kb_start(is_adm))
    await cb.answer()


@router.callback_query(F.data == "tos_decline")
async def tos_decline(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        "❌ Вы отказались от соглашения.\n"
        "Бот недоступен. Чтобы вернуться — нажмите /start и примите условия."
    )
    await state.clear()
    await cb.answer()


@router.callback_query(F.data == "back_start")
async def back_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    is_adm = await db.is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME, reply_markup=kb_start(is_adm))
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  Профиль
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    u = cb.from_user
    async with db.SessionLocal() as s:
        user = await db.get_or_create_user(s, u)
        await s.refresh(user)
        text = (
            f"👤 <b>Ваш профиль</b>\n\n"
            f"🆔 ID: <code>{u.id}</code>\n"
            f"📛 Имя: {user.full_name or '—'}\n"
            f"🔗 Username: @{user.username or '—'}\n"
            f"🛒 Покупок: <b>{user.purchases}</b>"
        )
    await cb.message.edit_text(text, reply_markup=kb_profile())
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  Каталог: страны → годы
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, state: FSMContext):
    countries = await db.countries()
    if not countries:
        await cb.message.edit_text("😔 Каталог пуст. Загляните позже!", reply_markup=kb_back_to_catalog())
        await state.clear()
        await cb.answer()
        return
    await cb.message.edit_text(
        f"🌍 <b>Каталог аккаунтов</b>\n\n"
        f"Доступно {len(countries)} {'страна' if len(countries) == 1 else 'стран'}. "
        f"Выберите нужную:",
        reply_markup=await kb_countries(),
    )
    await state.set_state(Buy.choose_country)
    await cb.answer()


@router.callback_query(F.data.startswith("c|"), StateFilter(Buy.choose_country))
async def cb_choose_country(cb: CallbackQuery, state: FSMContext):
    country = cb.data.split("|", 1)[1]
    years = await db.years(country)
    if not years:
        await cb.message.edit_text("😔 Нет доступных аккаунтов.", reply_markup=kb_back_to_catalog())
        await state.clear()
        await cb.answer()
        return
    await state.update_data(country=country)
    await cb.message.edit_text(
        f"📅 <b>Выберите год регистрации</b> (страна: {country}):",
        reply_markup=kb_years(years),
    )
    await state.set_state(Buy.choose_year)
    await cb.answer()


@router.callback_query(F.data.startswith("y|"), StateFilter(Buy.choose_year))
async def cb_choose_year(cb: CallbackQuery, state: FSMContext):
    year = int(cb.data.split("|", 1)[1])
    data = await state.get_data()
    country = data["country"]
    acc = await db.pick_account(country, year)
    if not acc:
        await cb.message.edit_text("😔 Аккаунты с такими параметрами закончились.", reply_markup=kb_back_to_catalog())
        await state.clear()
        await cb.answer()
        return
    from datetime import datetime
    age = datetime.now().year - year
    await state.update_data(year=year, account_id=acc.id, price=acc.price, phone=acc.phone)
    await cb.message.edit_text(
        f"📱 <b>Аккаунт найден!</b>\n\n"
        f"🌍 Страна: {country}\n"
        f"📅 Год: {year} ({age} {'год' if 1 < age < 5 else 'лет'})\n"
        f"💰 Цена: <b>{acc.price} руб.</b>\n\n"
        f"Выберите способ оплаты:",
        reply_markup=kb_payment(),
    )
    await state.set_state(Buy.choose_payment)
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  Оплата: Сбер (скрин)
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "pay_sber", StateFilter(Buy.choose_payment))
async def cb_pay_sber(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price = data.get("price", 0)
    await cb.message.edit_text(
        f"💳 <b>Оплата переводом на карту</b>\n\n"
        f"💰 Сумма: <b>{price} руб.</b>\n\n"
        f"🏦 Карта: <code>{SBER_CARD}</code>\n"
        f"👤 Получатель: <b>{SBER_RECIPIENT}</b>\n\n"
        f"⚠️ <b>Важно:</b>\n"
        f"• Сумма должна совпадать <b>точно</b>\n"
        f"• На скриншоте должно быть видно имя «Максим»\n"
        f"• Иначе — автоматический отказ\n\n"
        f"📤 Переведите и отправьте скриншот сюда:",
        reply_markup=kb_back_to_catalog(),
    )
    await state.set_state(Buy.wait_screenshot)
    await cb.answer()


@router.message(StateFilter(Buy.wait_screenshot), F.photo)
async def on_screenshot(msg: Message, state: FSMContext):
    """Пользователь прислал фото — качаем, прогоняем через OCR, сверяем."""
    data = await state.get_data()
    expected = int(data.get("price", 0))
    acc_id = data.get("account_id")
    if not acc_id:
        await msg.answer("⚠️ Сессия истекла. Начните заново: /start")
        await state.clear()
        return

    # 1) качаем фото
    photo = msg.photo[-1]
    file = await bot.download(photo.file_id, seek_to=0, destination=None)
    image_bytes = file.read()

    # 2) OCR
    wait_msg = await msg.answer("🔍 Проверяю скриншот…")
    ocr_result = await ocr.recognize_screenshot(image_bytes)
    verdict = ocr.check_payment(ocr_result, expected)
    log.info("OCR verdict: %s | raw: %s", verdict, ocr_result.get("raw_text", ""))

    await wait_msg.delete()

    # 3) разбираем вердикт
    if not verdict["valid"]:
        reason = verdict["reason"]
        if reason == "wrong_name":
            await msg.answer(
                f"❌ <b>Неверный получатель</b>\n\n"
                f"На скриншоте: <b>{verdict['detected']}</b>\n"
                f"Должно быть: <b>{SBER_RECIPIENT}</b>\n\n"
                f"Переведите на правильную карту и пришлите новый скриншот.",
                reply_markup=kb_back_to_catalog(),
            )
        elif reason == "wrong_amount":
            await msg.answer(
                f"❌ <b>Неверная сумма</b>\n\n"
                f"На скриншоте: <b>{verdict['detected']} руб.</b>\n"
                f"Ожидается: <b>{expected} руб.</b>",
                reply_markup=kb_back_to_catalog(),
            )
        elif reason == "no_amount":
            await msg.answer(
                "❌ <b>Не удалось распознать сумму</b>\n\n"
                "Пришлите более чёткий скриншот, где видна сумма перевода.",
                reply_markup=kb_back_to_catalog(),
            )
        else:
            await msg.answer(
                "❌ <b>Не удалось обработать скриншот</b>\n\n"
                "Попробуйте прислать изображение ещё раз.",
                reply_markup=kb_back_to_catalog(),
            )
        return

    # ✅ Всё ок — выдаём телефон, ждём код
    phone = data["phone"]
    await msg.answer(
        f"✅ <b>Оплата подтверждена!</b>\n\n"
        f"📱 <b>Ваш аккаунт:</b>\n"
        f"<code>{phone}</code>\n\n"
        f"📖 <b>Туториал по входу:</b>\n"
        f"1. Откройте <b>Настройки</b> Telegram\n"
        f"2. <b>Аккаунт</b> → <b>Добавить аккаунт</b>\n"
        f"3. Введите номер выше\n\n"
        f"📨 Сейчас на этот номер придёт код подтверждения.\n"
        f"<b>Пришлите его сюда — я активирую аккаунт.</b>"
    )
    await state.set_state(Buy.wait_code)


@router.message(StateFilter(Buy.wait_screenshot))
async def on_screenshot_bad(msg: Message):
    await msg.answer("📤 Пришлите именно <b>скриншот</b> (фото), не текст.")


@router.message(StateFilter(Buy.wait_code))
async def on_code(msg: Message, state: FSMContext):
    """Имитация приёма кода — принимаем любой текст, эхо-ответ, активация."""
    code = (msg.text or "").strip()
    if not re.fullmatch(r"[\d\- ]{3,12}", code):
        await msg.answer("❌ Код должен состоять из цифр. Пришлите код ещё раз.")
        return

    data = await state.get_data()
    acc_id = data.get("account_id")
    phone = data.get("phone")

    # 1) Удаляем аккаунт из БД
    if acc_id:
        await db.mark_sold(acc_id)
    # 2) +1 покупка юзеру
    await db.inc_purchases(msg.from_user.id)

    await msg.answer(
        f"✅ <b>Код принят: {code}</b>\n\n"
        f"🔄 Активирую аккаунт <code>{phone}</code>…\n\n"
        f"🎉 <b>Аккаунт успешно активирован!</b>\n\n"
        f"Спасибо за покупку. Возвращайтесь ещё 🙌"
    )
    is_adm = await db.is_admin(msg.from_user.id)
    await msg.answer(WELCOME, reply_markup=kb_start(is_adm))
    await state.clear()


# ════════════════════════════════════════════════════════════════
#  Оплата: Telegram Stars
# ════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "pay_stars", StateFilter(Buy.choose_payment))
async def cb_pay_stars(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price_stars = max(1, int(data.get("price", 1)))  # 1 рубль = 1 star, минимум 1
    acc_id = data.get("account_id")
    country = data.get("country", "")
    year = data.get("year", "")

    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"Аккаунт {country} {year}",
            description=f"Физический аккаунт Telegram — {country}, {year} год",
            payload=f"acc_{acc_id}",
            provider_token="",  # для Stars пусто
            currency="XTR",
            prices=[LabeledPrice(label="Аккаунт", amount=price_stars)],
        )
        await cb.answer()
    except Exception as e:
        log.exception("send_invoice")
        await cb.message.edit_text("❌ Ошибка при создании счёта. Попробуйте позже.", reply_markup=kb_back_to_catalog())
        await state.clear()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_stars_paid(msg: Message, state: FSMContext):
    payload = msg.successful_payment.invoice_payload or ""
    if not payload.startswith("acc_"):
        return
    acc_id = int(payload[4:])

    # Достаём данные аккаунта ДО удаления
    async with db.SessionLocal() as s:
        from sqlalchemy import select
        from database import Account
        r = await s.execute(select(Account).where(Account.id == acc_id))
        acc = r.scalar_one_or_none()
        if not acc:
            await msg.answer("⚠️ Аккаунт уже недоступен. Напишите в поддержку.")
            return
        phone = acc.phone
        country = acc.country
        year = acc.year
        await s.delete(acc)
        await s.commit()

    await db.inc_purchases(msg.from_user.id)

    await msg.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"📱 <b>Ваш аккаунт:</b>\n"
        f"<code>{phone}</code>\n\n"
        f"📖 <b>Туториал по входу:</b>\n"
        f"1. Откройте <b>Настройки</b> Telegram\n"
        f"2. <b>Аккаунт</b> → <b>Добавить аккаунт</b>\n"
        f"3. Введите номер выше\n\n"
        f"📨 Пришлите код подтверждения — я активирую аккаунт."
    )

    # Переводим в wait_code, сохранив данные
    await state.set_state(Buy.wait_code)
    await state.update_data(account_id=acc_id, phone=phone, country=country, year=year)


# ════════════════════════════════════════════════════════════════
#  Админ-панель
# ════════════════════════════════════════════════════════════════
def _admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID  # только главный


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    if not await db.is_admin(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminFSM.menu)
    await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=kb_admin())
    await cb.answer()


@router.callback_query(F.data == "adm_stats", StateFilter(AdminFSM.menu))
async def cb_stats(cb: CallbackQuery):
    if not await db.is_admin(cb.from_user.id):
        return
    st = await db.stats()
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{st['users']}</b>\n"
        f"🛒 Продано аккаунтов: <b>{st['sold']}</b>\n"
        f"📦 В наличии: <b>{st['available']}</b>"
    )
    await cb.message.edit_text(text, reply_markup=kb_admin())
    await cb.answer()


# ─── Добавить аккаунт ───
@router.callback_query(F.data == "adm_add_acc", StateFilter(AdminFSM.menu))
async def adm_add_acc(cb: CallbackQuery, state: FSMContext):
    if not await db.is_admin(cb.from_user.id):
        return
    await state.set_state(AdminFSM.add_phone)
    await cb.message.edit_text(
        "📞 <b>Шаг 1/4</b> — Введите номер телефона аккаунта:\n"
        "<i>Формат: 79161234567 (без +)</i>",
        reply_markup=kb_cancel_admin(),
    )
    await cb.answer()


@router.message(StateFilter(AdminFSM.add_phone))
async def adm_enter_phone(msg: Message, state: FSMContext):
    if not await db.is_admin(msg.from_user.id):
        return
    phone = (msg.text or "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.isdigit() or len(phone) < 7:
        await msg.answer("❌ Это не похоже на номер. Попробуйте ещё раз.")
        return
    await state.update_data(phone=phone)
    await state.set_state(AdminFSM.add_country)
    await msg.answer(
        "🌍 <b>Шаг 2/4</b> — Введите страну:\n"
        "<i>РФ, США, UK, Германия, Италия…</i>",
        reply_markup=kb_cancel_admin(),
    )


@router.message(StateFilter(AdminFSM.add_country))
async def adm_enter_country(msg: Message, state: FSMContext):
    if not await db.is_admin(msg.from_user.id):
        return
    country = (msg.text or "").strip()
    await state.update_data(country=country)
    await state.set_state(AdminFSM.add_year)
    from datetime import datetime
    await msg.answer(
        f"📅 <b>Шаг 3/4</b> — Введите год регистрации:\n"
        f"<i>Допустимо от 2010 до {datetime.now().year}</i>",
        reply_markup=kb_cancel_admin(),
    )


@router.message(StateFilter(AdminFSM.add_year))
async def adm_enter_year(msg: Message, state: FSMContext):
    if not await db.is_admin(msg.from_user.id):
        return
    try:
        year = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите год числом, например 2018")
        return
    from datetime import datetime
    if not (2010 <= year <= datetime.now().year):
        await msg.answer(f"❌ Год должен быть от 2010 до {datetime.now().year}")
        return
    await state.update_data(year=year)
    await state.set_state(AdminFSM.add_price)
    await msg.answer(
        "💰 <b>Шаг 4/4</b> — Введите цену в рублях:\n"
        "<i>Например: 250</i>",
        reply_markup=kb_cancel_admin(),
    )


@router.message(StateFilter(AdminFSM.add_price))
async def adm_enter_price(msg: Message, state: FSMContext):
    if not await db.is_admin(msg.from_user.id):
        return
    try:
        price = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите цену числом")
        return
    if price <= 0:
        await msg.answer("❌ Цена должна быть > 0")
        return

    data = await state.get_data()
    await db.add_account(data["phone"], data["country"], data["year"], price)
    await state.set_state(AdminFSM.menu)
    await msg.answer(
        f"✅ <b>Аккаунт добавлен!</b>\n\n"
        f"📞 {data['phone']}\n"
        f"🌍 {data['country']}\n"
        f"📅 {data['year']}\n"
        f"💰 {price} руб.",
        reply_markup=kb_admin(),
    )


# ─── Управление админами (только главный) ───
@router.callback_query(F.data == "adm_manage", StateFilter(AdminFSM.menu))
async def cb_adm_manage(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("⛔ Только для главного админа", show_alert=True)
        return
    await cb.message.edit_text("👑 <b>Управление админами</b>", reply_markup=kb_admins_manage())
    await cb.answer()


@router.callback_query(F.data == "adm_add_admin")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        return
    await state.set_state(AdminFSM.add_admin_id)
    await cb.message.edit_text(
        "👑 Введите Telegram ID нового админа:",
        reply_markup=kb_cancel_admin(),
    )
    await cb.answer()


@router.message(StateFilter(AdminFSM.add_admin_id))
async def adm_add_done(msg: Message, state: FSMContext):
    if not _admin_only(msg.from_user.id):
        return
    try:
        new_id = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите ID числом")
        return
    await db.add_admin(new_id)
    await state.set_state(AdminFSM.menu)
    await msg.answer(f"✅ Пользователь <code>{new_id}</code> назначен админом.", reply_markup=kb_admin())


@router.callback_query(F.data == "adm_remove_admin")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        return
    await state.set_state(AdminFSM.remove_admin_id)
    await cb.message.edit_text(
        "👑 Введите Telegram ID админа для снятия:",
        reply_markup=kb_cancel_admin(),
    )
    await cb.answer()


@router.message(StateFilter(AdminFSM.remove_admin_id))
async def adm_remove_done(msg: Message, state: FSMContext):
    if not _admin_only(msg.from_user.id):
        return
    try:
        rm_id = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("❌ Введите ID числом")
        return
    if rm_id == ADMIN_ID:
        await msg.answer("⛔ Главного админа нельзя снять.")
        await state.set_state(AdminFSM.menu)
        return
    await db.remove_admin(rm_id)
    await state.set_state(AdminFSM.menu)
    await msg.answer(f"✅ Пользователь <code>{rm_id}</code> снят с админки.", reply_markup=kb_admin())


@router.callback_query(F.data == "adm_list_admins")
async def cb_adm_list(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        return
    admins = await db.list_admins()
    text = "👑 <b>Список админов:</b>\n\n"
    text += f"• <code>{ADMIN_ID}</code> — главный (постоянный)\n"
    for a in admins:
        if a.id == ADMIN_ID:
            continue
        text += f"• <code>{a.id}</code> — @{a.username or '—'}\n"
    await cb.message.edit_text(text, reply_markup=kb_admins_manage())
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  Точка входа
# ════════════════════════════════════════════════════════════════
async def main():
    await db.init_db()
    log.info("Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
