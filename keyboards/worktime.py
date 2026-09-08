from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from keyboards.main_menu import BACK_TO_MAIN
from services.worktime_service import fmt_date_display, fmt_hours


def kb_worktime_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🕐 Сьогодні"), KeyboardButton(text="🗓 Історія годин")],
        [KeyboardButton(text="📊 Статистика годин")],
        [KeyboardButton(text=BACK_TO_MAIN)],
    ], resize_keyboard=True)


def kb_cancel_wt() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)


def ikb_stats_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сьогодні", callback_data="wtstat:today")],
        [InlineKeyboardButton(text="📆 7 днів", callback_data="wtstat:week")],
        [InlineKeyboardButton(text="🗓 Цей місяць", callback_data="wtstat:month")],
        [InlineKeyboardButton(text="✏️ Обрати період", callback_data="wtstat:custom")],
    ])


def ikb_history_list(entries: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = entries[start:start + per_page]
    rows = []
    for e in chunk:
        label = f"{fmt_date_display(e['date'])} — {fmt_hours(e['hours'])} год"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"wtview:{e['date']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"wtpage:{page-1}"))
    total_pages = max(1, (len(entries) - 1) // per_page + 1)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if start + per_page < len(entries):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"wtpage:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Додати запис за дату", callback_data="wtaddmanual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_entry_actions(date_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"wtedit:{date_str}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"wtdel:{date_str}"),
        ],
        [InlineKeyboardButton(text="◀️ До історії", callback_data="wtback_history")],
    ])


def ikb_delete_confirm(date_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"wtdelconfirm:{date_str}"),
            InlineKeyboardButton(text="❌ Ні", callback_data=f"wtview:{date_str}"),
        ],
    ])


def ikb_reminder_prompt(date_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ввести години", callback_data=f"wtenter:{date_str}")],
    ])