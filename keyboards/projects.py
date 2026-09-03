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
        [InlineKeyboardButton(text="🧩 Етапи проєкту", callback_data=f"projstages:{pid}")],
        [InlineKeyboardButton(text="📋 Задачі проєкту", callback_data=f"projtasks:{pid}")],
        [InlineKeyboardButton(text="💰 Бюджет проєкту", callback_data=f"projbudget:{pid}")],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"projtoggle:{pid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"projdel:{pid}"),
        ],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="projects_menu")],
    ])


def ikb_stages_list(pid: str, stages: list) -> InlineKeyboardMarkup:
    rows = []
    current_id = None
    for s in stages:
        if s.get("status") != "done":
            current_id = s.get("id")
            break
    for s in stages:
        if s.get("status") == "done":
            icon = "✅"
        elif s.get("id") == current_id:
            icon = "🔵"
        else:
            icon = "⏳"
        title = s.get("title", "")[:28]
        rows.append([InlineKeyboardButton(text=f"{icon} {title}", callback_data=f"stageopen:{pid}:{s['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Додати етап", callback_data=f"stage_add:{pid}")])
    rows.append([InlineKeyboardButton(text="✨ Створити етапи через AI", callback_data=f"stages_ai:{pid}")])
    rows.append([InlineKeyboardButton(text="◀️ До проєкту", callback_data=f"projopen:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_stage_actions(
    pid: str,
    stage_id: str,
    done: bool,
    can_up: bool = True,
    can_down: bool = True,
) -> InlineKeyboardMarkup:
    toggle_text = "↩️ Повернути в роботу" if done else "✅ Завершити етап"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data=f"stagetoggle:{pid}:{stage_id}")],
        [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"stageedit:{pid}:{stage_id}")],
    ]
    move_row = []
    if can_up:
        move_row.append(InlineKeyboardButton(text="⬆️", callback_data=f"stageup:{pid}:{stage_id}"))
    if can_down:
        move_row.append(InlineKeyboardButton(text="⬇️", callback_data=f"stagedown:{pid}:{stage_id}"))
    if move_row:
        rows.append(move_row)
    rows.append([InlineKeyboardButton(text="🗑 Видалити етап", callback_data=f"stagedel:{pid}:{stage_id}")])
    rows.append([InlineKeyboardButton(text="◀️ До етапів", callback_data=f"projstages:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_stages_ai_confirm(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зберегти ці етапи", callback_data=f"stages_ai_confirm:{pid}")],
        [InlineKeyboardButton(text="🔄 Згенерувати ще раз", callback_data=f"stages_ai:{pid}")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"stages_ai_cancel:{pid}")],
    ])