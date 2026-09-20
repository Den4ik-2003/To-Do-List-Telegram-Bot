

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.settings import EVENING_PLAN_HOUR_OPTIONS
from config.constants import LABELS


def ikb_evening_hours() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{h} год", callback_data=f"evplan_hours:{h}")
        for h in EVENING_PLAN_HOUR_OPTIONS
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="✏️ Інша кількість", callback_data="evplan_hours_custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_evening_generating() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="evplan_gen_cancel")],
    ])


def ikb_evening_plan_preview(tasks: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t.get("label", ""), {})
        rows.append([InlineKeyboardButton(
            text=f"{mark} {label.get('emoji','')} {i + 1}. {t['text'][:30]}",
            callback_data=f"evptoggle:{i}",
        )])
    rows.append([InlineKeyboardButton(text="✅ Підтвердити план", callback_data="evp_confirm")])
    rows.append([
        InlineKeyboardButton(text="🔄 Перегенерувати", callback_data="evp_regenerate"),
        InlineKeyboardButton(text="⏱ Змінити к-сть годин", callback_data="evp_change_hours"),
    ])
    rows.append([
        InlineKeyboardButton(text="➕ Додати завдання", callback_data="evp_add_task"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data="evp_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)