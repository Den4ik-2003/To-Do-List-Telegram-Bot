from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def kb_cancel_post() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)


def kb_photos_step() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✅ Готово")],
        [KeyboardButton(text="⏭ Без фото")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def ikb_shop_pick(shops: list, prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🏪 {s.get('title','')}", callback_data=f"{prefix}:{s['_id']}")] for s in shops]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_template_pick(templates: list, prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📄 {t.get('name','')}", callback_data=f"{prefix}:{t['_id']}")] for t in templates]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_post_preview(has_channel: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_channel:
        rows.append([InlineKeyboardButton(text="🚀 Опублікувати", callback_data="postpublish")])
    rows.append([InlineKeyboardButton(text="✏️ Редагувати текст", callback_data="postedittext")])
    rows.append([InlineKeyboardButton(text="🔄 Інший шаблон", callback_data="postretpl")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="postcancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_article_duplicate(article_id: str) -> InlineKeyboardMarkup:
    """Клавіатура дубліката САМЕ в флоу створення поста (окрема від standalone
    додавання в handlers/shop_articles.py, бо кнопка «Скасувати» тут веде
    в postcancel — скасування ВСЬОГО поста, а не тільки кроку з артикулом)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Переглянути товар", callback_data=f"artview:{article_id}")],
        [InlineKeyboardButton(text="🔄 Все одно додати", callback_data="artforce")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="postcancel")],
    ])