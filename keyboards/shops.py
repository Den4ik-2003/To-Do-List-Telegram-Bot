from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from keyboards.main_menu import BACK_TO_MAIN


def kb_shops_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Додати магазин")],
        [KeyboardButton(text=BACK_TO_MAIN)],
    ], resize_keyboard=True)


def kb_cancel_shop() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)


def ikb_shops_list(shops: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🏪 {s.get('title','')}", callback_data=f"shopopen:{s['_id']}")] for s in shops]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_shop_menu(shop_id: str, has_channel: bool) -> InlineKeyboardMarkup:
    channel_label = "📢 Канал: підключено" if has_channel else "📢 Підключити канал"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Шаблони", callback_data=f"shoptpls:{shop_id}")],
        [InlineKeyboardButton(text=channel_label, callback_data=f"shopchannel:{shop_id}")],
        [InlineKeyboardButton(text="✏️ Перейменувати", callback_data=f"shoprename:{shop_id}")],
        [InlineKeyboardButton(text="🗑 Видалити магазин", callback_data=f"shopdel:{shop_id}")],
        [InlineKeyboardButton(text="◀️ До магазинів", callback_data="shops_back")],
    ])


def ikb_shop_delete_confirm(shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"shopdelconfirm:{shop_id}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"shopopen:{shop_id}"),
    ]])


def ikb_channel_menu(shop_id: str, has_channel: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔗 Прив'язати/змінити канал", callback_data=f"shopchbind:{shop_id}")]]
    if has_channel:
        rows.append([InlineKeyboardButton(text="🔌 Відв'язати канал", callback_data=f"shopchunbind:{shop_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"shopopen:{shop_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)