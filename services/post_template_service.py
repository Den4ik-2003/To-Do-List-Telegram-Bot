import re

from services import ai_service

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def extract_placeholders(text: str) -> list:
    seen = []
    for name in PLACEHOLDER_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def render_template(text_template: str, fields: dict) -> str:
    def _sub(match):
        name = match.group(1)
        return str(fields.get(name, "") or "")
    return PLACEHOLDER_RE.sub(_sub, text_template)


async def analyze_example(raw_text: str) -> dict | None:
    if not ai_service.is_available():
        return None

    prompt = f"""Ти аналізуєш зразок посту для інтернет-магазину, щоб перетворити його на шаблон.

Ось приклад посту (збережи його ТОЧНУ структуру — усі емодзі, переноси рядків, порожні рядки, символи, порядок):

---
{raw_text}
---

Заміни у цьому тексті ЛИШЕ ті конкретні значення, які будуть різними для кожного нового поста
(наприклад: назва товару, колір, ціна, розмір, наявність, опис) на плейсхолдери у форматі {{{{назва_поля}}}}
(тільки латиниця, нижній регістр, без пробілів, англійською, коротко, напр. product, color, price, size, description).

НЕ змінюй емодзі, символи, порожні рядки, переноси рядків і статичний текст — вони мають лишитись ІДЕНТИЧНИМИ оригіналу.

Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown, у форматі:
{{"text_template": "текст із плейсхолдерами {{{{...}}}}", "placeholders": ["product", "color", "price"]}}"""

    data = await ai_service.generate_json(prompt, temperature=0.2)
    if not data or not data.get("text_template"):
        return None
    text_template = str(data["text_template"])
    placeholders = extract_placeholders(text_template)
    if not placeholders:
        return None
    return {"text_template": text_template, "placeholders": placeholders}


async def extract_fields(text_template: str, placeholders: list, user_text: str) -> dict:
    if not ai_service.is_available():
        return _fallback_extract(placeholders, user_text)

    fields_list = ", ".join(placeholders)
    prompt = f"""Ти заповнюєш шаблон поста даними з нового тексту від користувача.

Шаблон (з плейсхолдерами {{{{...}}}}):
---
{text_template}
---

Поля, які треба заповнити: {fields_list}

Новий текст від користувача:
---
{user_text}
---

Визнач значення кожного поля з нового тексту. Не вигадуй нічого — якщо якогось значення
немає в тексті користувача, постав null для цього поля.

Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown, у форматі:
{{"product": "значення або null", "color": "значення або null"}}"""

    data = await ai_service.generate_json(prompt, temperature=0.2)
    if not data:
        return _fallback_extract(placeholders, user_text)

    result = {}
    for name in placeholders:
        val = data.get(name)
        result[name] = str(val).strip() if val not in (None, "", "null") else None
    return result


def _fallback_extract(placeholders: list, user_text: str) -> dict:
    lines = [l.strip() for l in re.split(r"[\n,]", user_text) if l.strip()]
    result = {}
    for i, name in enumerate(placeholders):
        result[name] = lines[i] if i < len(lines) else None
    return result