from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db


# ─── /start ───
def kb_start(is_adm: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="catalog")],
    ]
    if is_adm:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Соглашение ───
def kb_tos() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю", callback_data="tos_accept")],
        [InlineKeyboardButton(text="❌ Отказываюсь", callback_data="tos_decline")],
    ])


# ─── Профиль ───
def kb_profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 В каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")],
    ])


# ─── Каталог: страны ───
async def kb_countries() -> InlineKeyboardMarkup:
    countries = await db.countries()
    builder = InlineKeyboardBuilder()
    for c in countries:
        builder.button(text=f"🌍 {c}", callback_data=f"c|{c}")
    builder.button(text="🔙 В меню", callback_data="back_start")
    builder.adjust(2)
    return builder.as_markup()


# ─── Каталог: годы ───
def kb_years(years: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for y in years:
        builder.button(text=f"📅 {y}", callback_data=f"y|{y}")
    builder.button(text="🔙 Назад", callback_data="catalog")
    builder.adjust(3)
    return builder.as_markup()


# ─── Выбор оплаты ───
def kb_payment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Сбер (по скриншоту)", callback_data="pay_sber")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="catalog")],
    ])


def kb_back_to_catalog() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В каталог", callback_data="catalog")],
    ])


# ─── Админ-панель ───
def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="adm_add_acc")],
        [InlineKeyboardButton(text="👑 Управление админами", callback_data="adm_manage")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_start")],
    ])


def kb_admins_manage() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add_admin")],
        [InlineKeyboardButton(text="➖ Снять админа", callback_data="adm_remove_admin")],
        [InlineKeyboardButton(text="📋 Список", callback_data="adm_list_admins")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")],
    ])


def kb_cancel_admin() -> InlineKeyboardMarkup:
    """Кнопка отмены любого админ-FSM."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
    ])
