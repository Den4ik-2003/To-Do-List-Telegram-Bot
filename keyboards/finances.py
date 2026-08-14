from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config.constants import INCOME_CATEGORIES, EXPENSE_CATEGORIES


def ikb_finance_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Дохід", callback_data="fin_add_income"),
            InlineKeyboardButton(text="➖ Витрата", callback_data="fin_add_expense"),
        ],
        [InlineKeyboardButton(text="📋 Історія", callback_data="fin_history")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="fin_stats")],
        [InlineKeyboardButton(text="🎯 Фінансові цілі", callback_data="fin_goals")],
        [InlineKeyboardButton(text="💳 Бюджети", callback_data="fin_budgets")],
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="fin_close")],
    ])


def kb_income_category() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=f"{c['emoji']} {c['name']}")] for c in INCOME_CATEGORIES.values()]
    rows.append([KeyboardButton(text="❌ Скасувати")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def income_category_from_text(text: str) -> str | None:
    for key, c in INCOME_CATEGORIES.items():
        if f"{c['emoji']} {c['name']}" == text:
            return key
    return None


def kb_expense_category() -> ReplyKeyboardMarkup:
    rows = []
    items = list(EXPENSE_CATEGORIES.values())
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        rows.append([KeyboardButton(text=f"{c['emoji']} {c['name']}") for c in pair])
    rows.append([KeyboardButton(text="❌ Скасувати")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def expense_category_from_text(text: str) -> str | None:
    for key, c in EXPENSE_CATEGORIES.items():
        if f"{c['emoji']} {c['name']}" == text:
            return key
    return None


def ikb_quick_transaction_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так", callback_data="quicktx_confirm"),
        InlineKeyboardButton(text="❌ Ні", callback_data="quicktx_cancel"),
    ]])


def ikb_transactions_list(transactions: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = transactions[start:start + per_page]
    rows = []
    for tx in chunk:
        sign = "+" if tx.get("type") == "income" else "-"
        rows.append([InlineKeyboardButton(
            text=f"{sign}{tx.get('amount',0):,.0f} грн — {tx.get('description','')[:20]}".replace(",", " "),
            callback_data=f"txopen:{tx['_id']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"txpage:{page-1}"))
    total_pages = max(1, (len(transactions) - 1) // per_page + 1)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if start + per_page < len(transactions):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"txpage:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fin_menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_budgets_list(budgets: list) -> InlineKeyboardMarkup:
    rows = []
    for b in budgets:
        bid = str(b["_id"])
        rows.append([InlineKeyboardButton(text=f"📦 {b.get('title','')[:24]}", callback_data=f"budgetopen:{bid}")])
    rows.append([InlineKeyboardButton(text="➕ Створити бюджет", callback_data="budget_add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fin_menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_budget_actions(bid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Змінити ліміт", callback_data=f"budgetedit:{bid}")],
        [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"budgetdel:{bid}")],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="fin_budgets")],
    ])