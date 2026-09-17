import asyncio
import logging
import re

import aiohttp

from services import ai_service
from config.settings import ORDER_WEBHOOK_BASE_URL

logger = logging.getLogger("tasks_bot")

MAX_SOURCE_HTML_CHARS = 12000
MAX_FILES = 15
MAX_FILE_CHARS = 40000
MAX_TOTAL_CHARS = 250000

MAX_INPUT_FILE_CHARS = 15000
MAX_INPUT_TOTAL_CHARS = 45000

_GENERATE_RETRIES = 2
_GENERATE_RETRY_DELAY_SEC = 2

_ALLOWED_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md"}


def slugify(text: str, fallback: str = "site") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:50] or fallback


def _sanitize_path(path: str) -> str | None:
    path = (path or "").strip().lstrip("/")
    if not path or ".." in path or path.startswith("."):
        return None
    if "." not in path.rsplit("/", 1)[-1]:
        return None
    ext = "." + path.rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_EXT:
        return None
    return path


def _parse_site_response(data: dict | None) -> dict | None:
    if not data or not isinstance(data.get("files"), dict) or not data["files"]:
        return None

    files: dict[str, str] = {}
    total_chars = 0
    for raw_path, content in data["files"].items():
        if len(files) >= MAX_FILES or total_chars >= MAX_TOTAL_CHARS:
            break
        path = _sanitize_path(raw_path)
        if not path or content is None:
            continue
        text = str(content)[:MAX_FILE_CHARS]
        files[path] = text
        total_chars += len(text)

    if "index.html" not in files:
        return None

    return {
        "summary": str(data.get("summary", "")).strip()[:500] or "Сайт згенеровано AI.",
        "site_name": slugify(str(data.get("site_name", "")).strip()),
        "commit_message": str(data.get("commit_message", "")).strip()[:200] or "AI website update",
        "files": files,
    }


def _build_files_block(current_files: dict[str, str]) -> str:
    parts: list[str] = []
    total_chars = 0
    for path, content in current_files.items():
        if total_chars >= MAX_INPUT_TOTAL_CHARS:
            parts.append(f"### {path}\n```\n[файл пропущено — досягнуто ліміту розміру промпту]\n```")
            continue
        text = content or ""
        truncated = len(text) > MAX_INPUT_FILE_CHARS
        text = text[:MAX_INPUT_FILE_CHARS]
        if truncated:
            text += "\n/* ...обрізано, файл довший... */"
        total_chars += len(text)
        parts.append(f"### {path}\n```\n{text}\n```")
    return "\n\n".join(parts)


def _checklist_block(checklist: list[str]) -> str:
    if not checklist:
        return ""
    items = "\n".join(f"- {c}" for c in checklist)
    return f"""
Обов'язковий чекліст вимог із ТЗ користувача — КОЖЕН пункт має бути
реалізований на сайті, не пропускай жодного:
{items}
"""


async def _generate_with_retry(
    prompt: str, temperature: float, images: list[str] | None = None, retries: int = _GENERATE_RETRIES,
) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            data = await ai_service.generate_json(prompt, temperature=temperature, images=images)
        except Exception as e:
            last_error = e
            logger.exception(
                "website_builder_service: ai_service.generate_json кинув виняток (спроба=%s/%s)",
                attempt, retries,
            )
            data = None

        if data:
            return data

        logger.warning(
            "website_builder_service: генерація повернула порожній результат (спроба=%s/%s)",
            attempt, retries,
        )
        if attempt < retries:
            await asyncio.sleep(_GENERATE_RETRY_DELAY_SEC)

    if last_error:
        logger.error("website_builder_service: усі спроби генерації провалились: %s", last_error)
    return None


_RESPONSE_FORMAT_RULES = """
Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown-розмітки, у форматі:
{
  "summary": "короткий опис українською, що це за сайт і що в ньому є",
  "site_name": "коротка kebab-case назва латиницею для домену/репозиторію, напр. athleon-store",
  "commit_message": "короткий commit message англійською, у стилі conventional commits",
  "files": {
    "index.html": "повний вміст файлу",
    "style.css": "повний вміст файлу",
    "script.js": "повний вміст файлу"
  }
}
Правила для файлів: чистий HTML/CSS/JS без збірки (без React/Vue/webpack) —
одразу відкриваються в браузері й працюють на статичному хостингу
(Netlify/GitHub Pages). index.html — повністю адаптивний (і мобільна версія).
Максимум 6 файлів. Стилі окремим style.css, скрипти окремим script.js
(підключені через <link>/<script src="">), inline лише по мінімуму.
Семантичний HTML, охайний сучасний дизайн.
"""

_ORDER_FORM_RULES = f"""
Додай на сторінку форму замовлення (секція "Замовити" або кнопка "Купити"
біля товару, що відкриває форму) з полями: ім'я (name), телефон (phone),
товар (product — якщо товарів кілька, підстав назву конкретного товару як
значення за замовчуванням або приховане поле), коментар (comment,
необов'язкове).

Форма не повинна перезавантажувати сторінку. У script.js додай обробник,
що при відправці робить:
fetch("{ORDER_WEBHOOK_BASE_URL}/order/__SITE_ID__", {{
  method: "POST",
  headers: {{"Content-Type": "application/json"}},
  body: JSON.stringify({{name, phone, product, comment}})
}})
і показує повідомлення про успішне надсилання (текст на сторінці, без
alert()). __SITE_ID__ лишай буквально як є (плейсхолдер підставить
сервер) — не вигадуй замість нього значення.
"""

_CHECKLIST_FORMAT_RULES = """
Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown-розмітки, у форматі:
{
  "checklist": ["конкретний, перевірюваний пункт вимоги 1", "пункт 2", ...],
  "clarifying_questions": ["питання до користувача, якщо щось незрозуміло", ...]
}
checklist — розбий ТЗ на короткі конкретні пункти (кожен — один блок/функція/
вимога), максимум 15 пунктів. clarifying_questions — лише якщо є пункти, які
неможливо однозначно реалізувати без уточнення (напр. не вказано кількість
товарів, мову сайту, наявність оплати тощо); якщо все зрозуміло — поверни
порожній список.
"""

_VERIFY_FORMAT_RULES = """
Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown-розмітки, у форматі:
{"missing": ["текст пункту чеклиста, який НЕ реалізовано", ...]}
Текст кожного пункту в "missing" має ТОЧНО збігатись з одним із пунктів
чеклиста нижче (копіюй один в один). Якщо реалізовано все — поверни
{"missing": []}.
"""


async def fetch_source_reference(url: str) -> dict | None:
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; AiWebsiteBuilder/1.0)"},
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")
    except Exception:
        logger.exception("fetch_source_reference failed for %s", url)
        return None

    return {"url": url, "html_excerpt": html[:MAX_SOURCE_HTML_CHARS]}


async def generate_landing_from_scratch(description: str, checklist: list[str] | None = None) -> dict | None:
    prompt = f"""Ти — AI-розробник, що створює лендінг-сторінку з нуля на HTML/CSS/JS. Відповідай українською (крім самого коду).

Опис продукту/магазину від користувача:
"{description}"

Створи повноцінний, візуально привабливий лендінг під цей опис (стиль обери сам,
якщо не вказано явно — сучасний, преміальний, з акуратною типографікою).
{_checklist_block(checklist or [])}
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.6)
    return _parse_site_response(data)


async def generate_clone_redesign(source_ref: dict, description: str, checklist: list[str] | None = None) -> dict | None:
    excerpt = source_ref.get("html_excerpt", "")[:3000]
    prompt = f"""Ти — AI-розробник, що створює ОРИГІНАЛЬНИЙ сайт, беручи за натхнення СТРУКТУРУ
іншого сайту. Відповідай українською (крім самого коду).

Нижче — уривок HTML стороннього сайту. Це ЛИШЕ референс стилю й структури
(які блоки є, як вони розташовані: шапка, hero-секція, сітка товарів, футер тощо).

КАТЕГОРИЧНО ЗАБОРОНЕНО:
- копіювати текст, зображення, код, назви або логотипи з референсу;
- відтворювати його один-в-один.

ОБОВ'ЯЗКОВО:
- взяти лише загальну ідею компонування/стилю;
- повністю переробити під продукт користувача нижче: інші кольори, тексти,
  назви блоків, логотип (текстовий, якщо іншого нема), контент, тематику.

Референс (структура/стиль, НЕ контент для копіювання):
{excerpt}

Завдання користувача (продукт + побажання по стилю):
"{description}"

{_checklist_block(checklist or [])}
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.6)
    return _parse_site_response(data)


async def refine_site(current_files: dict[str, str], instruction: str) -> dict | None:
    files_block = _build_files_block(current_files)
    prompt = f"""Ти — AI-розробник, що вносить правки в уже готовий сайт (HTML/CSS/JS). Відповідай українською (крім самого коду).

Поточний вміст сайту:

{files_block}

Завдання від користувача:
"{instruction}"

Внеси потрібні зміни. Поверни ПОВНИЙ оновлений набір файлів — тобто ВСІ файли
сайту (і змінені, і незмінені), а не тільки ті, що торкнулись правки. Якщо на
сайті вже є форма замовлення зі скриптом fetch(...) — НЕ видаляй і не ламай її,
навіть якщо завдання користувача її прямо не стосується.
{_RESPONSE_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.4)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result


async def generate_product_card_update(
    current_files: dict[str, str], product: dict, image_path: str,
) -> dict | None:
    files_block = _build_files_block(current_files)
    price_text = f"{product['price_uah']:.0f} грн" if product.get("price_uah") else "ціну уточнити"
    prompt = f"""Ти — AI-розробник, що додає ОДНУ нову картку товару в каталог/сітку товарів
на вже готовому сайті (HTML/CSS/JS). Відповідай українською (крім самого коду).

Поточний вміст сайту:

{files_block}

Новий товар для додавання:
- Назва: {product['title']}
- Опис: {product.get('description', '')}
- Категорія: {product.get('category', '')}
- Ціна: {price_text}
- Шлях до вже завантаженого фото (використай ЯК Є, не вигадуй інший шлях): {image_path}

Завдання:
- Знайди на сторінці секцію/сітку товарів (якщо її ще немає — створи мінімальну
  секцію "Товари" перед формою замовлення).
- Додай ОДНУ нову картку товару з фото (<img src="{image_path}">), назвою, ціною
  і коротким описом, у тому ж стилі, що й інші картки (якщо вони є).
- Кнопка/посилання картки має підставляти назву товару в поле "product" форми
  замовлення (якщо форма замовлення вже є на сторінці — не створюй другу форму).
- НІЧОГО ІНШОГО на сайті не міняй: не переписуй тексти, не міняй кольори,
  не видаляй існуючі товари чи секції.

Поверни ПОВНИЙ оновлений набір файлів (усі файли сайту, не тільки змінені).
{_RESPONSE_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.3)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result


async def analyze_requirements(description: str) -> dict | None:
    prompt = f"""Ти — AI-аналітик вимог для генератора сайтів. Проаналізуй
технічне завдання від користувача і розбий його на чіткий чекліст вимог.

ТЗ від користувача:
"{description}"

{_CHECKLIST_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.2)
    if not data or not isinstance(data.get("checklist"), list):
        return None
    checklist = [str(x).strip()[:200] for x in data["checklist"] if str(x).strip()][:15]
    if not checklist:
        return None
    questions = [str(x).strip()[:200] for x in data.get("clarifying_questions", []) if str(x).strip()][:5]
    return {"checklist": checklist, "clarifying_questions": questions}


async def verify_checklist(files: dict[str, str], checklist: list[str]) -> list[str]:
    if not checklist:
        return []
    files_block = _build_files_block(files)
    items = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(checklist))
    prompt = f"""Ти — AI-рев'юер сайтів. Перевір, чи реалізовано на сайті КОЖЕН
пункт чеклиста нижче.

Чекліст вимог:
{items}

Поточні файли сайту:
{files_block}

{_VERIFY_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.1)
    if not data or not isinstance(data.get("missing"), list):
        return []
    valid = {c.strip() for c in checklist}
    missing = [str(m).strip() for m in data["missing"] if str(m).strip()]
    filtered = [m for m in missing if m in valid]
    return filtered if filtered or not missing else missing[:10]


async def fix_missing_requirements(files: dict[str, str], missing: list[str]) -> dict | None:
    files_block = _build_files_block(files)
    items = "\n".join(f"- {m}" for m in missing)
    prompt = f"""Ти — AI-розробник, що доопрацьовує вже готовий сайт (HTML/CSS/JS). Відповідай українською (крім самого коду).

На сайті НЕ реалізовано такі пункти ТЗ:
{items}

Поточні файли сайту:
{files_block}

Доопрацюй сайт так, щоб усі ці пункти зʼявились, НЕ ламаючи решту сайту і
НЕ видаляючи вже готовий функціонал (форму замовлення, товари тощо).
Поверни ПОВНИЙ оновлений набір файлів.
{_RESPONSE_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.3)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result


async def generate_site_from_photos(
    images: list[str], description: str, checklist: list[str] | None = None,
) -> dict | None:
    extra = (
        f'Додаткові побажання від користувача:\n"{description}"'
        if description else "Додаткових текстових побажань нема — орієнтуйся лише на зображення."
    )
    prompt = f"""Ти — AI-розробник, що відтворює дизайн сайту за наданими скріншотами.
Відповідай українською (крім самого коду).

Проаналізуй ВСІ надані зображення разом. Якщо їх декілька — це послідовні
частини ОДНІЄЇ сторінки (згори вниз), об'єднай їх у ОДНУ цілісну сторінку в
правильному порядку блоків, без дублювання і розривів.

Визнач структуру сторінки: блоки, відступи, шрифти, кольори, кнопки, картки,
форми і загальний стиль, і відтвори сайт максимально близько до зображень.

{extra}
{_checklist_block(checklist or [])}
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.4, images=images)
    return _parse_site_response(data)