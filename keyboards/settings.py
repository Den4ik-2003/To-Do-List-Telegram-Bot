"""
ЗМІНЕНИЙ ФАЙЛ: keyboards/settings.py

Додано (для фічі "👥 Авторизовані користувачі"):
- ikb_settings_menu: нова кнопка "👥 Авторизовані користувачі"
  (callback_data="settings_users_list").
- ikb_users_list(users): список кнопок — одна на кожного авторизованого
  користувача, підписана його ІМ'ЯМ (не ID). users — список dict
  {"uid": int, "name": str}, формується в хендлері через bot.get_chat().
- ikb_block_confirm(uid, name): підтвердження "Так / Ні" перед блокуванням.

Решта — 1:1 як було.
"""

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config.constants import LABELS, CATEGORIES, STATUS_DONE
from utils.dates import is_missed


def ikb_settings_menu(
    morning_enabled: bool,
    evening_enabled: bool,
    notifications_enabled: bool,
    worktime_enabled: bool = True,
) -> InlineKeyboardMarkup:
    morning_label = "🔔 Ранковий план: Увімкнено" if morning_enabled else "🔕 Ранковий план: Вимкнено"
    evening_label = "🌙 Вечірній аналіз: Увімкнено" if evening_enabled else "🌙 Вечірній аналіз: Вимкнено"
    notif_label = "🔔 Сповіщення: Увімкнено" if notifications_enabled else "🔕 Сповіщення: Вимкнено"
    worktime_label = "🕐 Облік часу: Увімкнено" if worktime_enabled else "🕐 Облік часу: Вимкнено"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=morning_label, callback_data="settings_toggle_morning")],
        [InlineKeyboardButton(text="⏰ Час ранкового плану", callback_data="settings_morning_time")],
        [InlineKeyboardButton(text=evening_label, callback_data="settings_toggle_evening")],
        [InlineKeyboardButton(text="🤖 AI налаштування", callback_data="settings_ai")],
        [InlineKeyboardButton(text="📊 Ліміт AI-запитів", callback_data="settings_ai_limit")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="settings_currency")],
        [InlineKeyboardButton(text=notif_label, callback_data="settings_toggle_notifications")],
        [InlineKeyboardButton(text=worktime_label, callback_data="settings_toggle_worktime")],
        [InlineKeyboardButton(text="⏰ Час нагадування про облік часу", callback_data="settings_worktime_time")],
        [InlineKeyboardButton(text="👥 Авторизовані користувачі", callback_data="settings_users_list")],
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="settings_close")],
    ])


def kb_currency_select() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="₴ UAH"), KeyboardButton(text="$ USD"), KeyboardButton(text="€ EUR")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def currency_from_text(text: str) -> str | None:
    return {"₴ UAH": "UAH", "$ USD": "USD", "€ EUR": "EUR"}.get(text)


def ikb_archive_clear() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так", callback_data="archclr:yes"),
            InlineKeyboardButton(text="❌ Ні", callback_data="archclr:no"),
        ],
    ])


# =========================================================
# НОВЕ: 👥 Авторизовані користувачі (список + блокування по імені)
# =========================================================

def ikb_users_list(users: list) -> InlineKeyboardMarkup:
    """users: список {"uid": int, "name": str}. Текст кнопки — ІМ'Я,
    callback_data несе uid (технічна необхідність Telegram API), але
    юзер бачить і обирає саме за іменем."""
    rows = [
        [InlineKeyboardButton(text=f"🚫 {u['name']}", callback_data=f"settings_block_ask:{u['uid']}")]
        for u in users
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_block_confirm(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так, заблокувати", callback_data=f"settings_block_confirm:{uid}"),
            InlineKeyboardButton(text="❌ Ні", callback_data="settings_users_list"),
        ],
    ])