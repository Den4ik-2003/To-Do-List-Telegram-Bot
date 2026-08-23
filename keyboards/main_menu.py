from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Мої задачі"), KeyboardButton(text="🤖 AI Планер")],
        [KeyboardButton(text="💬 AI Чат"), KeyboardButton(text="🌐 Переклад")],
        [KeyboardButton(text="✍️ Редактор"), KeyboardButton(text="🗺 Що поруч")],
        [KeyboardButton(text="⚖️ Рішення"), KeyboardButton(text="📉 OLX Ціни")],
        [KeyboardButton(text="🎯 Мої цілі"), KeyboardButton(text="📁 Мої проєкти")],
        [KeyboardButton(text="💰 Фінанси"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="💱 Курс валют"), KeyboardButton(text="💰 Конвертер")],
        [KeyboardButton(text="📅 Дні до дати"), KeyboardButton(text="🌤️ Погода")],
        [KeyboardButton(text="🧾 Чек"), KeyboardButton(text="💡 Поради")],
        [KeyboardButton(text="⚙️ Налаштування")],
    ], resize_keyboard=True)


def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)


def kb_yes_no() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✅ Так"), KeyboardButton(text="❌ Ні")],
    ], resize_keyboard=True)


def ikb_back(callback_data: str, text: str = "◀️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]])


def ikb_confirm(yes_cb: str, no_cb: str, yes_text: str = "✅ Так", no_text: str = "❌ Ні") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=yes_text, callback_data=yes_cb),
        InlineKeyboardButton(text=no_text, callback_data=no_cb),
    ]])