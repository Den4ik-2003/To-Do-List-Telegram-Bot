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

Публічні сигнатури старих функцій (fetch_source_reference,
generate_landing_from_scratch, generate_clone_redesign, refine_site,
slugify) НЕ змінені.
"""

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
Правила для самих файлів:
- Чистий HTML/CSS/JS, БЕЗ збірки (без React/Vue/webpack) — файли мають одразу
  відкриватись у браузері й коректно працювати на статичному хостингу (Netlify/GitHub Pages).
- Обов'язково зроби index.html повністю адаптивним (мобільна версія теж).
- Максимум 6 файлів. Стилі — окремим style.css, скрипти — окремим script.js
  (підключені через <link>/<script src="">), inline лише по мінімуму.
- Семантичний HTML, сучасний, охайний дизайн (нормальні відступи, читабельна типографіка).
"""

# НОВЕ: інструкція про форму замовлення. __SITE_ID__ — плейсхолдер,
# підміняється в handlers/website_builder.py одразу після першого
# збереження сайту в БД (коли з'являється реальний db_id).
_ORDER_FORM_RULES = f"""
Обов'язково додай на сторінку форму замовлення (наприклад секцію "Замовити"
або кнопку "Купити" біля кожного товару, що відкриває форму) з полями:
ім'я (name), телефон (phone), товар (product — якщо товарів кілька, підстав
назву конкретного товару як значення за замовчуванням або приховане поле),
коментар (comment, необов'язкове).

Форма НЕ повинна перезавантажувати сторінку. Додай у script.js обробник, що
при відправці робить:
fetch("{ORDER_WEBHOOK_BASE_URL}/order/__SITE_ID__", {{
  method: "POST",
  headers: {{"Content-Type": "application/json"}},
  body: JSON.stringify({{name, phone, product, comment}})
}})
і показує користувачу повідомлення про успішне надсилання (просто текст на
сторінці, без alert()). __SITE_ID__ лишай ЯК Є буквально в коді (це
плейсхолдер, який підставить сервер) — НЕ вигадуй замість нього значення.
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
    data = await ai_service.generate_json(prompt, temperature=0.6)
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
    data = await ai_service.generate_json(prompt, temperature=0.6)
    return _parse_site_response(data)


async def refine_site(current_files: dict[str, str], instruction: str) -> dict | None:
    files_block = "\n\n".join(
        f"### {path}\n```\n{content}\n```" for path, content in current_files.items()
    )
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
    data = await ai_service.generate_json(prompt, temperature=0.4)
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
    files_block = "\n\n".join(
        f"### {path}\n```\n{content}\n```" for path, content in current_files.items()
    )
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
    data = await ai_service.generate_json(prompt, temperature=0.3)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result