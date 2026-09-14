"""
НОВИЙ ФАЙЛ: services/product_asset_service.py

Обробка фото товару для AI Website Builder (пункт 2 ТЗ):
- стискає/масштабує фото (Pillow), щоб не роздувати репозиторій/Netlify-zip;
- генерує стабільний шлях assets/products/<slug>.jpg;
- НЕ пише нічого в MongoDB — сам байт-контент живе лише в пам'яті процесу
  (services/website_builder.py._pending_product), поки не буде закомічений
  у GitHub/Netlify тим самим шляхом, що й решта файлів сайту.
- для розпізнавання товару перевикористовує services/ai_service.analyze_product_photo
  (ТОЙ САМИЙ виклик, що вже юзає handlers/product_photo.py) — нового AI-промпту
  для "що на фото" не вигадуємо.
"""

import io
import logging
import re

from services import ai_service
from config.settings import MAX_PRODUCT_IMAGE_BYTES, PRODUCT_IMAGE_MAX_DIM

logger = logging.getLogger("tasks_bot")

_PRICE_RE = re.compile(r"(\d[\d\s]{1,9})\s*(грн|uah|₴)", re.IGNORECASE)


def slugify(text: str, fallback: str = "product") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or fallback


def optimize_image(image_bytes: bytes) -> bytes:
    """Best-effort стиснення через Pillow. Якщо Pillow недоступний або
    сталась помилка обробки — повертає оригінал як є (краще великий
    файл, ніж зламаний деплой)."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow не встановлено — фото товару буде закомічено без оптимізації")
        return image_bytes[:MAX_PRODUCT_IMAGE_BYTES] if len(image_bytes) > MAX_PRODUCT_IMAGE_BYTES else image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img.thumbnail((PRODUCT_IMAGE_MAX_DIM, PRODUCT_IMAGE_MAX_DIM))
        out = io.BytesIO()
        quality = 85
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        while len(data) > MAX_PRODUCT_IMAGE_BYTES and quality > 40:
            quality -= 10
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            data = out.getvalue()
        return data
    except Exception:
        logger.exception("Не вдалося оптимізувати фото товару, використовую оригінал")
        return image_bytes


def build_asset_path(product_name: str, uid: int) -> str:
    return f"assets/products/{slugify(product_name)}-{uid % 100000}.jpg"


def _extract_price_from_text(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", ""))
    except ValueError:
        return None


async def parse_product_submission(image_bytes: bytes, caption: str) -> dict:
    """Розпізнає товар з фото (через ai_service.analyze_product_photo,
    той самий виклик, що й у 📷 Фото → Товар) і домішує явні дані з
    підпису користувача (ціна/назва мають пріоритет над AI-здогадкою,
    якщо користувач їх вказав прямим текстом).

    Повертає {"title", "description", "category", "price_uah",
    "missing": [список полів, яких бракує]}."""
    ai_data = await ai_service.analyze_product_photo(image_bytes) or {}

    title = ai_data.get("title", "").strip()
    description = ai_data.get("description", "").strip()
    category = ai_data.get("category", "").strip()
    price = ai_data.get("price_uah")

    caption = (caption or "").strip()
    caption_price = _extract_price_from_text(caption)
    if caption_price is not None:
        price = caption_price

    if caption and not title:
        # якщо AI взагалі не розпізнав товар, перший рядок підпису
        # використовуємо як робочу назву замість повної відмови
        title = caption.split("\n")[0][:80]

    missing = []
    if not title:
        missing.append("title")
    if not price:
        missing.append("price_uah")

    return {
        "title": title,
        "description": description or caption,
        "category": category or "Інше",
        "price_uah": price,
        "missing": missing,
    }