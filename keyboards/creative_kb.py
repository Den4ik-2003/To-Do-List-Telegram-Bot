from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

FORMATS = {
    "1:1": (1024, 1024),
    "16:9": (1536, 1024),
    "9:16": (1024, 1536),
    "4:5": (1024, 1280),  # генеруємо 1024x1536 і кропаємо під 4:5
}

STYLES = {
    "photo": "📷 Реалістичне",
    "illustration": "🎨 Ілюстрація",
    "3d": "🧸 3D",
    "cinematic": "🎬 Кінематографічне",
    "anime": "🎌 Anime",
    "art": "🖌️ Арт",
    "instagram": "📱 Instagram",
}

STYLE_PROMPTS = {
    "photo": "photorealistic, high detail, natural lighting",
    "illustration": "digital illustration, clean lines, vibrant colors",
    "3d": "3D render, soft studio lighting, pixar-like style",
    "cinematic": "cinematic shot, dramatic lighting, film grain, wide angle",
    "anime": "anime style, cel shading, vivid colors",
    "art": "fine art painting style, expressive brush strokes",
    "instagram": "bright, trendy, high-contrast social media aesthetic",
}

TEMPLATES = {
    "avatar": "📸 Аватарка",
    "cover": "🖼️ Обкладинка",
    "ig_post": "📱 Instagram Post",
    "ig_story": "📱 Instagram Story",
    "thumbnail": "🎬 Thumbnail",
    "meme": "😂 Мем",
    "product": "🛍️ Фото товару",
    "sticker": "🎨 Стікер",
    "emoji": "😀 Emoji",
    "holiday": "🎁 Святкова картинка",
}


def kb_formats() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in FORMATS:
        b.button(text=key, callback_data=f"cs_fmt:{key}")
    b.adjust(4)
    return b.as_markup()


def kb_styles() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label in STYLES.items():
        b.button(text=label, callback_data=f"cs_style:{key}")
    b.button(text="➡️ Без стилю", callback_data="cs_style:none")
    b.adjust(2)
    return b.as_markup()


def kb_templates() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label in TEMPLATES.items():
        b.button(text=label, callback_data=f"cs_tpl:{key}")
    b.adjust(2)
    return b.as_markup()


def kb_regenerate(gen_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Перегенерувати", callback_data=f"cs_regen:{gen_id}")
    b.button(text="✅ Зберегти", callback_data=f"cs_save:{gen_id}")
    b.adjust(2)
    return b.as_markup()