from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config.constants import PRIORITY_EMOJI


def kb_priority() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Високий"), KeyboardButton(text="🟡 Середній"), KeyboardButton(text="🟢 Низький")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def priority_from_text(text: str) -> str | None:
    return {"🔴 Високий": "high", "🟡 Середній": "medium", "🟢 Низький": "low"}.get(text)


def kb_goal_type() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💰 Фінансова ціль")],
        [KeyboardButton(text="✅ Звичайна ціль")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def goal_type_from_text(text: str) -> str | None:
    return {"💰 Фінансова ціль": "financial", "✅ Звичайна ціль": "simple"}.get(text)


def ikb_goals(goals: list) -> InlineKeyboardMarkup:
    rows = []
    for g in goals:
        gid = str(g["_id"])
        toggle_text = "⏸ Деактивувати" if g.get("active") else "▶️ Активувати"
        pr = PRIORITY_EMOJI.get(g.get("priority", "medium"), "🟡")
        title = g.get("title", "")[:24]
        rows.append([InlineKeyboardButton(text=f"{pr} {title}", callback_data=f"goalopen:{gid}")])
        rows.append([
            InlineKeyboardButton(text=toggle_text, callback_data=f"goaltoggle:{gid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"goaldel:{gid}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Додати ціль", callback_data="goal_add")])
    rows.append([InlineKeyboardButton(text="◀️ Головне меню", callback_data="goals_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_goal_actions(gid: str, active: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Деактивувати" if active else "▶️ Активувати"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Оновити суму", callback_data=f"goalupdate:{gid}")],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"goaltoggle:{gid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"goaldel:{gid}"),
        ],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="ai_goals")],
    ])