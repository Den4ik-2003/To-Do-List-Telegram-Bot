from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import PROJECT_ACTIVE


def ikb_projects(projects: list) -> InlineKeyboardMarkup:
    rows = []
    for p in projects:
        pid = str(p["_id"])
        is_active = p.get("status") == PROJECT_ACTIVE
        status_icon = "🟢" if is_active else "✅"
        title = p.get("title", "")[:24]
        rows.append([InlineKeyboardButton(text=f"{status_icon} {title}", callback_data=f"projopen:{pid}")])
    rows.append([InlineKeyboardButton(text="➕ Додати проєкт", callback_data="proj_add")])
    rows.append([InlineKeyboardButton(text="◀️ Головне меню", callback_data="proj_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_project_actions(pid: str, active: bool) -> InlineKeyboardMarkup:
    toggle_text = "✅ Завершити" if active else "▶️ Відновити"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Задачі проєкту", callback_data=f"projtasks:{pid}")],
        [InlineKeyboardButton(text="💰 Бюджет проєкту", callback_data=f"projbudget:{pid}")],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"projtoggle:{pid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"projdel:{pid}"),
        ],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="projects_menu")],
    ])