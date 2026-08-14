from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def ikb_settings_menu(morning_enabled: bool, evening_enabled: bool, notifications_enabled: bool) -> InlineKeyboardMarkup:
    morning_label = "🔔 Ранковий план: Увімкнено" if morning_enabled else "🔕 Ранковий план: Вимкнено"
    evening_label = "🌙 Вечірній аналіз: Увімкнено" if evening_enabled else "🌙 Вечірній аналіз: Вимкнено"
    notif_label = "🔔 Сповіщення: Увімкнено" if notifications_enabled else "🔕 Сповіщення: Вимкнено"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=morning_label, callback_data="settings_toggle_morning")],
        [InlineKeyboardButton(text="⏰ Час ранкового плану", callback_data="settings_morning_time")],
        [InlineKeyboardButton(text=evening_label, callback_data="settings_toggle_evening")],
        [InlineKeyboardButton(text="🤖 AI налаштування", callback_data="settings_ai")],
        [InlineKeyboardButton(text="📊 Ліміт AI-запитів", callback_data="settings_ai_limit")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="settings_currency")],
        [InlineKeyboardButton(text=notif_label, callback_data="settings_toggle_notifications")],
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