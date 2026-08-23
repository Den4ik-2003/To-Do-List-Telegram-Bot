from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# Мапа: текст кнопки-категорії -> список кнопок у її підменю.
# Використовується і для побудови клавіатур, і в handlers/menu.py для
# розпізнавання натискання категорії та повернення потрібного підменю.
CATEGORY_TASKS_AI = "📝 Задачі та AI"
CATEGORY_FINANCE = "💵 Гроші та ціни"
CATEGORY_GOALS = "🎯 Цілі та проєкти"
CATEGORY_LIFE = "🌍 Побут"

MAIN_CATEGORIES = {
    CATEGORY_TASKS_AI: [
        "📋 Мої задачі", "🤖 AI Планер",
        "💬 AI Чат", "✍️ Редактор",
        "🌐 Переклад", "⚖️ Рішення",
    ],
    CATEGORY_FINANCE: [
        "💰 Фінанси", "📊 Статистика",
        "💱 Курс валют", "💰 Конвертер",
        "🧾 Чек", "📉 OLX Ціни",
    ],
    CATEGORY_GOALS: [
        "🎯 Мої цілі", "📁 Мої проєкти",
    ],
    CATEGORY_LIFE: [
        "🗺 Що поруч", "🌤️ Погода",
        "📅 Дні до дати", "💡 Поради",
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
    """Підменю конкретної категорії + кнопка повернення в головне меню."""
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