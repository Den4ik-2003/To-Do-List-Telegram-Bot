

import logging

import aiohttp

from config.settings import CLOUDINARY_CLOUD_NAME, CLOUDINARY_UPLOAD_PRESET

logger = logging.getLogger("tasks_bot")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


def is_available() -> bool:
    return bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_UPLOAD_PRESET)


async def upload_image(image_bytes: bytes, public_id: str | None = None) -> str | None:
    """Заливає ОДНЕ вже оптимізоване (JPEG) фото в Cloudinary. Повертає
    постійний secure_url або None — виклик має обробити None, не падаючи
    з винятком (сайт лишається без фото, але не ламається генерація)."""
    if not is_available():
        logger.warning(
            "Cloudinary не налаштовано (CLOUDINARY_CLOUD_NAME/CLOUDINARY_UPLOAD_PRESET) — фото не завантажено"
        )
        return None

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    form = aiohttp.FormData()
    form.add_field("file", image_bytes, filename="upload.jpg", content_type="image/jpeg")
    form.add_field("upload_preset", CLOUDINARY_UPLOAD_PRESET)
    if public_id:
        form.add_field("public_id", public_id)

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(url, data=form) as resp:
                data = await resp.json(content_type=None)
                if resp.status not in (200, 201):
                    logger.error("Cloudinary upload failed: %s %s", resp.status, str(data)[:500])
                    return None
                secure_url = data.get("secure_url") or data.get("url")
                if not secure_url:
                    logger.error("Cloudinary upload: відповідь без secure_url: %s", str(data)[:500])
                    return None
                return secure_url
    except Exception:
        logger.exception("Cloudinary upload crashed")
        return None