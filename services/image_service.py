import base64
import logging

import aiohttp

from config.settings import IMAGE_API_KEY, IMAGE_BASE_URL, IMAGE_GEN_MODEL, IMAGE_REQUEST_TIMEOUT

logger = logging.getLogger("tasks_bot")


class ImageServiceError(Exception):
    pass


def _headers() -> dict:
    if not IMAGE_API_KEY:
        raise ImageServiceError("IMAGE_API_KEY не заданий у env")
    return {"Authorization": f"Bearer {IMAGE_API_KEY}"}


async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    transparent: bool = False,
    quality: str = "medium",
) -> bytes:
    """Текст → зображення. Повертає байти PNG."""
    payload = {
        "model": IMAGE_GEN_MODEL,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "quality": quality,
    }
    if transparent:
        payload["background"] = "transparent"

    timeout = aiohttp.ClientTimeout(total=IMAGE_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(
                f"{IMAGE_BASE_URL}/images/generations",
                json=payload,
                headers=_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error("Image gen failed: %s", data)
                    raise ImageServiceError(data.get("error", {}).get("message", "Помилка генерації"))
                b64 = data["data"][0]["b64_json"]
                return base64.b64decode(b64)
        except aiohttp.ClientError as e:
            logger.exception("Image gen network error")
            raise ImageServiceError(f"Мережева помилка: {e}") from e


async def edit_image(
    image_bytes: bytes,
    prompt: str,
    size: str = "1024x1024",
    transparent: bool = False,
    filename: str = "input.png",
) -> bytes:
    """Фото + інструкція → відредаговане зображення."""
    form = aiohttp.FormData()
    form.add_field("model", IMAGE_GEN_MODEL)
    form.add_field("prompt", prompt)
    form.add_field("size", size)
    if transparent:
        form.add_field("background", "transparent")
    form.add_field("image", image_bytes, filename=filename, content_type="image/png")

    timeout = aiohttp.ClientTimeout(total=IMAGE_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(
                f"{IMAGE_BASE_URL}/images/edits",
                data=form,
                headers=_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error("Image edit failed: %s", data)
                    raise ImageServiceError(data.get("error", {}).get("message", "Помилка редагування"))
                b64 = data["data"][0]["b64_json"]
                return base64.b64decode(b64)
        except aiohttp.ClientError as e:
            logger.exception("Image edit network error")
            raise ImageServiceError(f"Мережева помилка: {e}") from e