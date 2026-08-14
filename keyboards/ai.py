from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import LABELS, CATEGORIES


def ikb_ai_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Створити план на сьогодні", callback_data="ai_plan")],
        [InlineKeyboardButton(text="🔄 Оновити план", callback_data="ai_regenerate")],
        [InlineKeyboardButton(text="📋 Мій план", callback_data="ai_current_plan")],
        [InlineKeyboardButton(text="📊 Аналіз продуктивності", callback_data="ai_analysis")],
        [InlineKeyboardButton(text="⚙️ Налаштування планера", callback_data="ai_settings")],
        [InlineKeyboardButton(text="◀️ Головне меню", callback_data="ai_close")],
    ])


def ikb_ai_plan_preview(tasks: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t.get("label", ""), {})
        rows.append([InlineKeyboardButton(text=f"{mark} {label.get('emoji','')} {t['text'][:35]}", callback_data=f"aiptoggle:{i}")])
    rows.append([
        InlineKeyboardButton(text="✅ Додати всі", callback_data="aip_select_all"),
        InlineKeyboardButton(text="🔄 Перегенерувати", callback_data="ai_regenerate"),
    ])
    rows.append([
        InlineKeyboardButton(text="🚀 Створити план", callback_data="aip_add"),
        InlineKeyboardButton(text="❌ Не додавати", callback_data="aip_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_ai_settings(morning_enabled: bool) -> InlineKeyboardMarkup:
    label = "🔔 Ранковий план: Увімкнено" if morning_enabled else "🔕 Ранковий план: Вимкнено"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data="ai_toggle_morning")],
        [InlineKeyboardButton(text="⏰ Час ранкового плану", callback_data="ai_set_morning_time")],
        [InlineKeyboardButton(text="📊 Ліміт AI-запитів", callback_data="ai_usage_info")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ai_menu_back")],
    ])


def ikb_ai_usage(used: int, limit: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤖 Використано: {used}/{limit}", callback_data="noop")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ai_menu_back")],
    ])


def ikb_chat_context_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Проаналізуй мій тиждень", callback_data="chat_quick:week")],
        [InlineKeyboardButton(text="🎯 Я відстаю від цілі?", callback_data="chat_quick:goal_progress")],
        [InlineKeyboardButton(text="💰 Чи варто зараз витрачати гроші?", callback_data="chat_quick:spend_advice")],
        [InlineKeyboardButton(text="◀️ Завершити чат", callback_data="chat_close")],
    ])