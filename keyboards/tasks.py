from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config.constants import LABELS, CATEGORIES, STATUS_DONE
from utils.dates import is_missed


def kb_tasks_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Додати задачу"), KeyboardButton(text="📋 Сьогодні")],
        [KeyboardButton(text="📅 Майбутні"), KeyboardButton(text="✅ Виконані")],
        [KeyboardButton(text="⭐ Обране"), KeyboardButton(text="🏷 Категорії")],
        [KeyboardButton(text="◀️ Головне меню")],
    ], resize_keyboard=True)


def kb_label() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Терміново"), KeyboardButton(text="🟡 Середньо")],
        [KeyboardButton(text="🟢 Не поспішає"), KeyboardButton(text="🔵 Ідея")],
        [KeyboardButton(text="🟣 Особисте")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def label_from_text(text: str) -> str | None:
    mapping = {
        "🔴 Терміново": "urgent", "🟡 Середньо": "medium", "🟢 Не поспішає": "low",
        "🔵 Ідея": "idea", "🟣 Особисте": "personal",
    }
    return mapping.get(text)


def kb_category() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💻 Робота"), KeyboardButton(text="💰 Фінанси")],
        [KeyboardButton(text="🏠 Дім"), KeyboardButton(text="💪 Спорт")],
        [KeyboardButton(text="📚 Навчання"), KeyboardButton(text="🗂 Інше")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def category_from_text(text: str) -> str | None:
    mapping = {
        "💻 Робота": "work", "💰 Фінанси": "finance", "🏠 Дім": "home",
        "💪 Спорт": "sport", "📚 Навчання": "study", "🗂 Інше": "other",
    }
    return mapping.get(text)


def kb_date() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Сьогодні"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="✏️ Своя дата (дд.мм.рррр)")],
        [KeyboardButton(text="⏭ Без терміну")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def ikb_task_actions(tid: int, t: dict) -> InlineKeyboardMarkup:
    if t.get("pinned"):
        pin_btn = InlineKeyboardButton(text="📌 Відкріпити", callback_data=f"unpin:{tid}")
    else:
        pin_btn = InlineKeyboardButton(text="⭐ Закріпити", callback_data=f"pin:{tid}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [pin_btn, InlineKeyboardButton(text="📝 Підзадачі", callback_data=f"subtasks:{tid}")],
        [
            InlineKeyboardButton(text="🕐 +1 год", callback_data=f"postp1h:{tid}"),
            InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"edit:{tid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"deltask:{tid}"),
        ],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="back_to_list")],
    ])


def ikb_rollover_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [InlineKeyboardButton(text="🕐 Через 1 год", callback_data=f"postp1h:{tid}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}")],
        [InlineKeyboardButton(text="❌ Видалити", callback_data=f"deltask:{tid}")],
    ])


def ikb_reminder_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
    ])


def ikb_edit_fields(tid: int) -> InlineKeyboardMarkup:
    fields = [
        ("text", "📝 Текст"),
        ("label", "🎨 Мітка"),
        ("category", "🏷 Категорія"),
        ("date", "📅 Дата"),
        ("time", "🕐 Час"),
        ("subtasks_add", "➕ Додати підзадачі"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"editfield:{tid}:{key}")] for key, label in fields]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_tasks_list(tasks: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = tasks[start:start + per_page]
    rows = []
    for t in chunk:
        label = LABELS.get(t.get("label", "idea"), {"emoji": ""})
        status_icon = "✅" if t.get("status") == STATUS_DONE else ("⚠️" if is_missed(t) else "⏳")
        pin_str = "📌" if t.get("pinned") else ""
        due_short = t.get("due", "")[-5:] if t.get("due") else "без терм."
        lbl = f"{status_icon} {pin_str}{label['emoji']} №{t['id']} {due_short} {t.get('text','')[:18]}"
        rows.append([InlineKeyboardButton(text=lbl, callback_data=f"view:{t['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page-1}"))
    total_pages = max(1, (len(tasks) - 1) // per_page + 1)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if start + per_page < len(tasks):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_categories() -> InlineKeyboardMarkup:
    rows = []
    for key, cat in CATEGORIES.items():
        rows.append([InlineKeyboardButton(text=f"{cat['emoji']} {cat['name']}", callback_data=f"catopen:{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tasks_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)