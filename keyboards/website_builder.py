"""
ЗМІНЕНИЙ ФАЙЛ: keyboards/website_builder.py

Додано відносно попередньої версії:
- ikb_wb_result(): нові кнопки "🖼 Додати товар" і "📦 Замовлення"
  (обидві потребують, щоб сайт уже мав db_id, бо це дії над збереженим
  сайтом — цю перевірку робить хендлер, а не клавіатура).
- ikb_wb_product_confirm(): підтвердження перед комітом товару (щоб
  дотриматись пункту ТЗ "після підтвердження оновити сайт").
"""

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def ikb_wb_result(has_github: bool, has_netlify: bool, has_db_id: bool = False) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👀 Переглянути", callback_data="wb_preview"))
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
    b.row(InlineKeyboardButton(text="✏️ Переробити", callback_data="wb_refine_start"))
    if has_db_id:
        b.row(
            InlineKeyboardButton(text="🖼 Додати товар", callback_data="wb_product_start"),
            InlineKeyboardButton(text="📦 Замовлення", callback_data="wb_orders_view"),
        )
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