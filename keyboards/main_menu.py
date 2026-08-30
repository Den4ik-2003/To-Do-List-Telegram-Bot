from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

CATEGORY_TASKS_AI = "📝 Задачі та AI"
CATEGORY_FINANCE = "💵 Гроші та ціни"
CATEGORY_GOALS = "🎯 Цілі та проєкти"
CATEGORY_LIFE = "🌍 Побут"
CATEGORY_BUSINESS = "💼 Бізнес"
CATEGORY_JOBS = "💼 Вакансії"

MAIN_CATEGORIES = {
    CATEGORY_TASKS_AI: [
        "📋 Мої задачі", "🤖 AI Планер",
        "💬 AI Чат", "✍️ Редактор",
        "🌐 Переклад", "⚖️ Рішення",
        "🎬 Що подивитися сьогодні",
    ],
    CATEGORY_FINANCE: [
        "💰 Фінанси", "📊 Статистика",
        "💱 Курс валют", "💰 Конвертер",
        "🧾 Чек", "📉 OLX Ціни",
        "🚗 Авто", "📷 Фото → Товар",
    ],
    CATEGORY_GOALS: [
        "🎯 Мої цілі", "📁 Мої проєкти",
    ],
    CATEGORY_LIFE: [
        "🗺 Що поруч", "🌤️ Погода",
        "📅 Дні до дати", "💡 Поради",
        "🌐 Моніторинг сайтів",
    ],
    CATEGORY_BUSINESS: [
        "🔎 Знайти перепродаж", "💡 Зробити з ідеї бізнес",
        "⭐ Збережені можливості", "📊 Мої бізнес-ідеї",
    ],
    CATEGORY_JOBS: [
        "🔎 Знайти вакансії", "👤 Мої дані для пошуку",
        "⭐ Збережені вакансії", "🔔 Мої монітори вакансій",
    ],
}

BACK_TO_MAIN = "⬅️ Головне меню"


def _rows_of_two(items: list[str]) -> list[list[KeyboardButton]]:
    rows = []
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        rows.append([KeyboardButton(text=t) for t in pair])
    return rows


def kb_main() -> ReplyKeyboardMarkup:
    keyboard = _rows_of_two(list(MAIN_CATEGORIES.keys()))
    keyboard.append([KeyboardButton(text="⚙️ Налаштування")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def kb_category(category: str) -> ReplyKeyboardMarkup:
    items = MAIN_CATEGORIES.get(category, [])
    keyboard = _rows_of_two(items)
    keyboard.append([KeyboardButton(text=BACK_TO_MAIN)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


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