"""
ЗМІНЕНИЙ ФАЙЛ: services/website_builder_service.py

Додано відносно попередньої версії:
1. _ORDER_FORM_RULES — інструкція для AI вбудовувати форму замовлення +
   JS, що шле POST на ORDER_WEBHOOK_BASE_URL/order/{site_id} (новий
   webhook у main.py). Додається до generate_landing_from_scratch/
   generate_clone_redesign (там, де site_id ще невідомий на момент
   генерації, підставляється плейсхолдер "__SITE_ID__", який хендлер
   підміняє на реальний db_id ПІСЛЯ першого збереження в БД — див.
   handlers/website_builder.py._inject_site_id()).
2. generate_product_card_update() — спеціалізований refine під фото
   товару (пункт 2 ТЗ): на відміну від refine_site() тут AI явно
   інструктовано ТІЛЬКИ додати картку товару з готовими даними й шляхом
   до вже завантаженого зображення, не чіпаючи решту сайту.
3. НОВЕ (стабільність генерації): _build_files_block() обрізає вміст
   кожного файлу перед вставкою в промпт refine_site/
   generate_product_card_update, щоб не роздувати запит до AI (великий
   промпт підвищує шанс таймауту/порожньої відповіді від безкоштовної
   моделі). _generate_with_retry() додає локальний повторний виклик
   ai_service.generate_json, якщо той повернув None (а не лише коли
   кинув виняток) — раніше такі випадки одразу призводили до провалу
   генерації сайту з першої ж невдалої спроби.

Публічні сигнатури старих функцій (fetch_source_reference,
generate_landing_from_scratch, generate_clone_redesign, refine_site,
slugify) НЕ змінені.
"""

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

# НОВЕ: обмеження на вміст файлів, які вставляються у ВХІДНИЙ промпт
# (refine_site / generate_product_card_update). Це окреме, менше
# обмеження від MAX_FILE_CHARS вище (те стосується файлів, які AI
# ПОВЕРТАЄ у відповіді). Мета — не давати моделі занадто великий
# контекст, бо це і сповільнює генерацію, і збільшує шанс таймауту чи
# порожньої/обрізаної відповіді від безкоштовної моделі.
MAX_INPUT_FILE_CHARS = 15000
MAX_INPUT_TOTAL_CHARS = 45000

# НОВЕ: скільки разів локально повторити виклик ai_service.generate_json,
# якщо той повернув None (наприклад через таймаут або порожню відповідь
# моделі, яку ai_service вже не зміг розпарсити навіть у текстовому
# fallback-режимі). Це саме локальний retry поверх того, що вже є
# всередині ai_service — тут ми просто пробуємо ще раз, а не одразу
# показуємо користувачу помилку.
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
    """Формує текстовий блок з поточними файлами сайту для вставки в
    промпт, обрізаючи занадто великі файли, щоб не роздувати запит до
    AI (див. MAX_INPUT_FILE_CHARS / MAX_INPUT_TOTAL_CHARS вище)."""
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


async def _generate_with_retry(prompt: str, temperature: float, retries: int = _GENERATE_RETRIES) -> dict | None:
    """Обгортка над ai_service.generate_json з локальним повторним
    викликом, якщо результат порожній (None). ai_service сам вміє
    ретраїти окремий HTTP-запит, але якщо ВСІ його спроби провалились
    (таймаут / порожня відповідь моделі навіть у текстовому fallback),
    він повертає None — тут ми пробуємо запустити генерацію ще раз
    з нуля, замість того щоб одразу здаватись."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            data = await ai_service.generate_json(prompt, temperature=temperature)
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

# НОВЕ: інструкція про форму замовлення. __SITE_ID__ — плейсхолдер,
# підміняється в handlers/website_builder.py одразу після першого
# збереження сайту в БД (коли з'являється реальний db_id).
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


async def generate_landing_from_scratch(description: str) -> dict | None:
    prompt = f"""Ти — AI-розробник, що створює лендінг-сторінку з нуля на HTML/CSS/JS. Відповідай українською (крім самого коду).

Опис продукту/магазину від користувача:
"{description}"

Створи повноцінний, візуально привабливий лендінг під цей опис (стиль обери сам,
якщо не вказано явно — сучасний, преміальний, з акуратною типографікою).
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.6)
    return _parse_site_response(data)


async def generate_clone_redesign(source_ref: dict, description: str) -> dict | None:
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

Завдання користувача (продукт + побажання по стилю):
"{description}"

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


# ============================================================
# НОВЕ: Додавання товару з фото (пункт 2 ТЗ)
# ============================================================

async def generate_product_card_update(
    current_files: dict[str, str], product: dict, image_path: str,
) -> dict | None:
    """Додає картку товару на вже готовий сайт. На відміну від refine_site
    тут завдання ЖОРСТКО обмежене — тільки додати картку з переданими
    даними, нічого іншого на сайті не міняти."""
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