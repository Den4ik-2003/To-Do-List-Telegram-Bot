"""
НОВИЙ ФАЙЛ: keyboards/website_builder.py

Вхід у фічу тепер іде через звичайну reply-категорію головного меню
(CATEGORY_WEBSITE у keyboards/main_menu.py), тому окремого inline-меню
("ikb_wb_menu") тут більше немає — лишились тільки клавіатури для самого
процесу генерації/деплою/списку сайтів.
"""

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def ikb_wb_result(has_github: bool, has_netlify: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👀 Переглянути", callback_data="wb_preview"))
    b.row(
        InlineKeyboardButton(
            text="🚀 Deploy GitHub" if not has_github else "🚀 Оновити GitHub",
            callback_data="wb_deploy_gh",
        ),
        InlineKeyboardButton(
            text="🌐 Deploy Netlify" if not has_netlify else "🌐 Оновити Netlify",
            callback_data="wb_deploy_netlify",
        ),
    )
    b.row(InlineKeyboardButton(text="✏️ Переробити", callback_data="wb_refine_start"))
    b.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="wb_cancel"))
    return b.as_markup()


def ikb_wb_sites_list(sites: list[dict]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for s in sites:
        title = s.get("siteName") or "Без назви"
        b.row(InlineKeyboardButton(text=f"🌐 {title}", callback_data=f"wb_site_open:{s['_id']}"))
    return b.as_markup()