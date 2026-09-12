from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def kb_cancel_tpl() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)


def kb_sticker_step() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⏭ Без стікера")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def ikb_templates_list(shop_id: str, templates: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📄 {t.get('name','')}", callback_data=f"tplopen:{t['_id']}")] for t in templates]
    rows.append([InlineKeyboardButton(text="➕ Додати шаблон", callback_data=f"tpladd:{shop_id}")])
    rows.append([InlineKeyboardButton(text="◀️ До магазину", callback_data=f"shopopen:{shop_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_template_actions(template_id: str, shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Застосувати", callback_data=f"posttplquick:{shop_id}:{template_id}")],
        [InlineKeyboardButton(text="✏️ Редагувати текст", callback_data=f"tpledit:{template_id}")],
        [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"tpldel:{template_id}")],
        [InlineKeyboardButton(text="◀️ До шаблонів", callback_data=f"shoptpls:{shop_id}")],
    ])


def ikb_template_confirm(shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зберегти як є", callback_data="tplconfirm")],
        [InlineKeyboardButton(text="✏️ Виправити вручну", callback_data="tplmanual")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"shoptpls:{shop_id}")],
    ])


def ikb_template_delete_confirm(template_id: str, shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так", callback_data=f"tpldelconfirm:{template_id}:{shop_id}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"tplopen:{template_id}"),
    ]])