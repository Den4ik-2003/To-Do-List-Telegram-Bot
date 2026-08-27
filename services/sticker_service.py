import io

from PIL import Image

STICKER_SIZE = 512
EMOJI_SIZE = 100


def _fit_square(image_bytes: bytes, target: int) -> bytes:
    """Ресайзить у прозорий квадрат target x target, зберігаючи пропорції."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    img.thumbnail((target, target), Image.LANCZOS)

    canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    x = (target - img.width) // 2
    y = (target - img.height) // 2
    canvas.paste(img, (x, y), img)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def prepare_sticker(image_bytes: bytes) -> bytes:
    return _fit_square(image_bytes, STICKER_SIZE)


def prepare_emoji(image_bytes: bytes) -> bytes:
    return _fit_square(image_bytes, EMOJI_SIZE)


def crop_to_aspect(image_bytes: bytes, target_w: int, target_h: int) -> bytes:
    """Кроп під точне співвідношення сторін (для 4:5 тощо)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    src_ratio = img.width / img.height
    target_ratio = target_w / target_h

    if src_ratio > target_ratio:
        new_w = int(img.height * target_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()