from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_github_settings(connected: bool) -> InlineKeyboardMarkup:
    if connected:
        rows = [
            [InlineKeyboardButton(text="🔍 Перевірити підключення", callback_data="ghset_check")],
            [InlineKeyboardButton(text="🔌 Відключити GitHub", callback_data="ghset_disconnect")],
        ]
    else:
        rows = [[InlineKeyboardButton(text="🔐 Підключити GitHub", callback_data="ghset_connect")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_disconnect_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, відключити", callback_data="ghset_disconnect_yes"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="ghset_disconnect_no"),
    ]])


def ikb_repo_choice(repos: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📁 {r['full_name']}", callback_data=f"ghrepo:{i}")]
        for i, r in enumerate(repos[:20])
    ]
    rows.append([InlineKeyboardButton(text="✏️ Ввести GitHub URL вручну", callback_data="ghrepo_manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_deploy_confirm(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Deploy", callback_data=f"gh_deploy_yes:{kind}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"gh_deploy_no:{kind}"),
    ]])


def ikb_projects_list(projects: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🟢 {p['projectName']}", callback_data=f"ghproj:{p['_id']}")]
        for p in projects
    ]
    rows.append([InlineKeyboardButton(text="➕ Додати проєкт", callback_data="ghproj_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_project_actions(project_id) -> InlineKeyboardMarkup:
    pid = str(project_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Deploy оновлення", callback_data=f"ghproj_deploy:{pid}")],
        [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"ghproj_edit:{pid}")],
        [InlineKeyboardButton(text="📜 Історія", callback_data=f"ghproj_history:{pid}")],
        [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"ghproj_del:{pid}")],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="ghproj_back")],
    ])


def ikb_project_delete_confirm(project_id) -> InlineKeyboardMarkup:
    pid = str(project_id)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"ghproj_del_yes:{pid}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"ghproj_del_no:{pid}"),
    ]])


def ikb_edit_fields(project_id) -> InlineKeyboardMarkup:
    pid = str(project_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Назва", callback_data=f"ghedit_name:{pid}")],
        [InlineKeyboardButton(text="🔗 GitHub URL", callback_data=f"ghedit_url:{pid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ghproj:{pid}")],
    ])