
from aiogram.types import InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def ikb_wb_result(has_github: bool, has_netlify: bool, has_db_id: bool = False) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👀 Переглянути", callback_data="wb_preview"),
        InlineKeyboardButton(text="📦 Завантажити ZIP", callback_data="wb_download_zip"),
    )
    b.row(
        InlineKeyboardButton(
            text="🚀 Deploy GitHub" if not has_github else "🐙 Оновити GitHub",
            callback_data="wb_deploy_gh",
        ),
        InlineKeyboardButton(
            text="🌐 Deploy Netlify" if not has_netlify else "🌐 Оновити Netlify",
            callback_data="wb_deploy_netlify",
        ),
    )
    b.row(InlineKeyboardButton(text="🛠 Виправити / Редагувати сайт", callback_data="wb_refine_start"))
    if has_db_id:
        b.row(
            InlineKeyboardButton(text="🖼 Додати товар", callback_data="wb_product_start"),
            InlineKeyboardButton(text="📦 Замовлення", callback_data="wb_orders_view"),
        )
        b.row(
            InlineKeyboardButton(text="🕐 Історія версій", callback_data="wb_history_view"),
            InlineKeyboardButton(text="🔍 Перевірити ТЗ", callback_data="wb_checklist_check"),
        )
        b.row(InlineKeyboardButton(text="💾 Зберегти як шаблон", callback_data="wb_save_as_template"))
        b.row(InlineKeyboardButton(text="📨 Бот для замовлень", callback_data="wb_bot_manage"))
        b.row(InlineKeyboardButton(text="🗑 Видалити", callback_data="wb_delete_start"))
    b.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="wb_cancel"))
    return b.as_markup()


def ikb_wb_sites_list(sites: list[dict]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for s in sites:
        title = s.get("siteName") or "Без назви"
        b.row(InlineKeyboardButton(text=f"🌐 {title}", callback_data=f"wb_site_open:{s['_id']}"))
    return b.as_markup()


def ikb_wb_product_confirm() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Додати", callback_data="wb_product_confirm"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="wb_product_cancel"),
    )
    return b.as_markup()


def ikb_wb_delete_confirm() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Так, видалити назавжди", callback_data="wb_delete_confirm"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="wb_delete_cancel"),
    )
    return b.as_markup()


def ikb_wb_history(site_id: str, versions: list[dict]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for i, v in enumerate(versions):
        ts = (v.get("savedAt") or "")[:16].replace("T", " ")
        summary = (v.get("summary") or "")[:40]
        b.row(InlineKeyboardButton(text=f"⏪ {ts} — {summary}", callback_data=f"wb_history_restore:{i}"))
    b.row(InlineKeyboardButton(text="❌ Закрити", callback_data="wb_history_close"))
    return b.as_markup()


def ikb_wb_bot_manage(is_connected: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="🔄 Змінити бота" if is_connected else "🔌 Підключити бота",
        callback_data="wb_bot_connect_start",
    ))
    if is_connected:
        b.row(InlineKeyboardButton(text="🔕 Відключити", callback_data="wb_bot_disconnect"))
    b.row(InlineKeyboardButton(text="❌ Закрити", callback_data="wb_bot_manage_close"))
    return b.as_markup()


def ikb_wb_templates_list(templates: list[dict]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for t in templates:
        name = t.get("name") or "Без назви"
        b.row(
            InlineKeyboardButton(text=f"📦 {name}", callback_data=f"wb_tpl_use:{t['_id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"wb_tpl_del:{t['_id']}"),
        )
    b.row(InlineKeyboardButton(text="➕ Завантажити новий шаблон", callback_data="wb_tpl_new"))
    b.row(InlineKeyboardButton(text="❌ Закрити", callback_data="wb_tpl_close"))
    return b.as_markup()


def ikb_wb_template_delete_confirm(template_id: str) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"wb_tpl_del_yes:{template_id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="wb_tpl_del_no"),
    )
    return b.as_markup()


def kb_photo_done() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Готово")], [KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )