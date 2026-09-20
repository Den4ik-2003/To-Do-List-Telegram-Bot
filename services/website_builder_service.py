import asyncio
import logging
import re
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from services import ai_service
from config.settings import ORDER_WEBHOOK_BASE_URL

logger = logging.getLogger("tasks_bot")

MAX_FILES = 15
MAX_FILE_CHARS = 40000
MAX_TOTAL_CHARS = 250000

MAX_INPUT_FILE_CHARS = 15000
MAX_INPUT_TOTAL_CHARS = 45000

_GENERATE_RETRIES = 2
_GENERATE_RETRY_DELAY_SEC = 2

_ALLOWED_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md"}

_FETCH_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (compatible; AiWebsiteBuilder/2.0; +https://example.com/bot)",
]

# Скільки зовнішніх CSS-файлів максимум тягнемо для одного клону і який
# загальний обсяг тексту з них використовуємо для аналізу кольорів/шрифтів.
MAX_CSS_FILES = 5
MAX_CSS_TOTAL_CHARS = 60000
_CSS_FETCH_TIMEOUT_SEC = 8


class CloneFetchError(Exception):
    def __init__(self, reason: str, user_message: str):
        self.reason = reason
        self.user_message = user_message
        super().__init__(reason)


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
Семантичний HTML, охайний сучасний дизайн. Обов'язково додай
<meta name="viewport" content="width=device-width, initial-scale=1">.

ПРО ЗОБРАЖЕННЯ (дуже важливо): якщо для картинки НЕМАЄ реального
завантаженого файлу користувача — НІКОЛИ не вигадуй локальний шлях типу
"hero.jpg", "images/photo.png", "assets/banner.webp" тощо. Такого файлу
не існує на сервері, і зображення буде банально битим (сірий значок
замість фото). Замість цього використовуй ГОТОВЕ робоче посилання на
стоковий сервіс:
- https://picsum.photos/seed/<будь-яке-унікальне-слово-по-темі>/1200/800
  — гарантовано робочий fallback для будь-якого декоративного фото;
- або https://images.unsplash.com/photo-<валідний-id>?auto=format&fit=crop&w=1200&q=80,
  якщо впевнений у конкретному id.
Якщо сумніваєшся — завжди бери picsum.photos, він ніколи не буває битим.
Це правило стосується ЛИШЕ нових декоративних/контентних зображень.
Посилання й шляхи на зображення, які ВЖЕ є в наданих файлах сайту чи шаблону
(у тому числі локальні), не чіпай і не переписуй. Фото товарів, які
користувач додає окремою функцією «Додати товар», уже матимуть реальний URL
(Cloudinary) — його підставляй як є, нічого не вигадуючи замість нього.
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

_QUALITY_CHECK_FORMAT_RULES = """
Поверни ВИКЛЮЧНО JSON без markdown-розмітки, у форматі:
{"issues": ["короткий опис проблеми 1", ...]}
Шукай: незакриті/розірвані HTML-теги, відсутній viewport meta, посилання
src/href на файли, яких немає серед наданих (биті шляхи), кнопки та форми
без реального обробника/дії, відсутність адаптивних стилів (media queries)
при складному layout, очевидні JS-помилки (звернення до неоголошених
змінних, синтаксичні помилки, незакриті дужки). Якщо проблем немає —
поверни {"issues": []}.
"""


async def _fetch_html(url: str) -> tuple[str | None, str | int | None]:
    last_status: int | None = None
    for ua in _FETCH_UAS:
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": ua, "Accept-Language": "uk,en;q=0.8"},
                    allow_redirects=True,
                ) as resp:
                    last_status = resp.status
                    if resp.status == 200:
                        html = await resp.text(errors="ignore")
                        return html, str(resp.url)
        except Exception:
            logger.exception("fetch_source_reference: fetch failed for %s (ua=%s...)", url, ua[:20])
            continue
    return None, last_status


async def _fetch_external_css(html: str, base_url: str) -> str:
    """Тягне до MAX_CSS_FILES зовнішніх <link rel="stylesheet"> файлів
    сайту, щоб кольори/шрифти визначались не лише з inline-стилів у
    HTML, а й з реальних CSS-файлів (де вони найчастіше й лежать).
    Best-effort: будь-яка проблема з окремим файлом просто пропускається,
    не ламаючи весь клон."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    hrefs: list[str] = []
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if any("stylesheet" in r.lower() for r in rel):
            hrefs.append(link["href"])
        if len(hrefs) >= MAX_CSS_FILES:
            break

    if not hrefs:
        return ""

    parts: list[str] = []
    total_chars = 0
    ua = _FETCH_UAS[0]
    timeout = aiohttp.ClientTimeout(total=_CSS_FETCH_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for href in hrefs:
            if total_chars >= MAX_CSS_TOTAL_CHARS:
                break
            css_url = urljoin(base_url, href)
            try:
                async with session.get(css_url, headers={"User-Agent": ua}) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text(errors="ignore")
            except Exception:
                logger.info("fetch_source_reference: не вдалося завантажити CSS %s", css_url)
                continue
            remaining = MAX_CSS_TOTAL_CHARS - total_chars
            text = text[:remaining]
            parts.append(text)
            total_chars += len(text)

    return "\n".join(parts)


def _extract_colors(html: str) -> list[str]:
    colors = set()
    for m in re.finditer(r"#(?:[0-9a-fA-F]{3}){1,2}\b", html):
        colors.add(m.group(0).lower())
    for m in re.finditer(r"rgba?\([^)]+\)", html):
        colors.add(m.group(0))
    return list(colors)[:12]


def _extract_fonts(soup: BeautifulSoup, html: str) -> list[str]:
    fonts = set()
    for link in soup.find_all("link", href=True):
        href = link["href"]
        if "fonts.googleapis.com" in href:
            m = re.search(r"family=([^&:]+)", href)
            if m:
                fonts.add(m.group(1).replace("+", " "))
    for m in re.finditer(r"font-family\s*:\s*([^;\"}]+)", html):
        fonts.add(m.group(1).strip())
    return list(fonts)[:8]


def _extract_structure(soup: BeautifulSoup) -> dict:
    def _text(tag):
        if not tag:
            return ""
        return re.sub(r"\s+", " ", tag.get_text(" ", strip=True))[:200]

    title = _text(soup.find("title"))
    headings = []
    for level in ("h1", "h2", "h3"):
        for tag in soup.find_all(level)[:6]:
            headings.append({"level": level, "text": _text(tag)})

    header = soup.find("header") or soup.find(attrs={"class": re.compile(r"header|navbar", re.I)})
    footer = soup.find("footer") or soup.find(attrs={"class": re.compile(r"footer", re.I)})
    nav_links = []
    nav = soup.find("nav")
    if nav:
        for a in nav.find_all("a")[:10]:
            text = _text(a)
            if text:
                nav_links.append(text)

    buttons = []
    for tag in soup.find_all(["button", "a"], limit=40):
        cls = " ".join(tag.get("class", []))
        if re.search(r"btn|button|cta", cls, re.I):
            text = _text(tag)
            if text:
                buttons.append(text)

    sections = []
    for tag in soup.find_all(["section", "div"], limit=200):
        cls = " ".join(tag.get("class", []))
        if re.search(r"hero|banner", cls, re.I):
            sections.append({"type": "hero"})
        elif re.search(r"product|card|item", cls, re.I):
            sections.append({"type": "card_grid"})
        elif re.search(r"testimonial|review", cls, re.I):
            sections.append({"type": "testimonials"})
    sections = sections[:10]

    images = len(soup.find_all("img"))
    forms = len(soup.find_all("form"))
    scripts = len(soup.find_all("script"))
    body = soup.find("body")
    body_text_len = len(_text(body)) if body else 0
    js_heavy = scripts > 3 and body_text_len < 200

    return {
        "title": title,
        "headings": headings[:15],
        "header_present": bool(header),
        "footer_present": bool(footer),
        "nav_links": nav_links,
        "buttons": buttons[:10],
        "sections": sections,
        "images_count": images,
        "forms_count": forms,
        "js_heavy": js_heavy,
        "body_text_len": body_text_len,
    }


async def fetch_source_reference(url: str) -> dict:
    html, status_or_url = await _fetch_html(url)

    if html is None:
        status = status_or_url
        if status == 404:
            raise CloneFetchError("not_found", "⚠️ Сторінку не знайдено (404). Перевір URL і спробуй ще раз.")
        if status == 403:
            raise CloneFetchError(
                "forbidden",
                "⚠️ Сайт заблокував автоматичний доступ (403). Спробуй інший URL або скористайся "
                "режимом «🖼 Сайт із фото» — надішли скріншоти сторінки.",
            )
        if isinstance(status, int) and status >= 500:
            raise CloneFetchError(
                "server_error", f"⚠️ Сайт тимчасово недоступний (помилка {status}). Спробуй ще раз пізніше.",
            )
        raise CloneFetchError(
            "network",
            "⚠️ Не вдалося з'єднатися із сайтом. Перевір, що URL починається з http:// або https:// "
            "і що сайт зараз доступний.",
        )

    final_url = status_or_url
    try:
        soup = BeautifulSoup(html, "html.parser")
        structure = _extract_structure(soup)
        css_text = await _fetch_external_css(html, final_url)
        style_source = html + "\n" + css_text if css_text else html
        colors = _extract_colors(style_source)
        fonts = _extract_fonts(soup, style_source)
    except Exception:
        logger.exception("fetch_source_reference: parsing failed for %s", url)
        structure, colors, fonts, css_text = {}, [], [], ""

    return {
        "url": final_url,
        "structure": structure,
        "colors": colors,
        "fonts": fonts,
        "js_heavy": bool(structure.get("js_heavy")),
        "css_fetched": bool(css_text),
    }


def clone_warning_message(source_ref: dict) -> str | None:
    structure = source_ref.get("structure") or {}
    if source_ref.get("js_heavy"):
        return (
            "⚠️ Сайт активно використовує JavaScript для завантаження контенту. Я отримав загальну "
            "структуру (заголовки, кольори, шрифти), але частина контенту могла бути недоступна без "
            "рендерингу браузером. Створюю версію на основі доступних даних."
        )
    if structure.get("images_count", 0) == 0 and structure.get("body_text_len", 0) < 100:
        return "⚠️ На сторінці знайдено дуже мало контенту. Створюю сайт на основі структури і твого опису."
    return None


def _structure_block(source_ref: dict) -> str:
    structure = source_ref.get("structure") or {}
    lines = [f"URL: {source_ref.get('url', '')}"]
    if structure.get("title"):
        lines.append(f"Заголовок сторінки: {structure['title']}")
    if structure.get("header_present"):
        lines.append("Є header/навігація")
    if structure.get("nav_links"):
        lines.append("Пункти меню: " + ", ".join(structure["nav_links"]))
    if structure.get("headings"):
        h = "; ".join(f"{x['level']}: {x['text']}" for x in structure["headings"][:8])
        lines.append(f"Заголовки блоків: {h}")
    if structure.get("sections"):
        s = ", ".join(x["type"] for x in structure["sections"])
        lines.append(f"Виявлені типи секцій: {s}")
    if structure.get("buttons"):
        lines.append("Тексти кнопок: " + ", ".join(structure["buttons"][:6]))
    if structure.get("footer_present"):
        lines.append("Є footer")
    lines.append(f"Кількість зображень: {structure.get('images_count', 0)}")
    lines.append(f"Кількість форм: {structure.get('forms_count', 0)}")
    if source_ref.get("colors"):
        lines.append("Кольори з CSS: " + ", ".join(source_ref["colors"][:10]))
    if source_ref.get("fonts"):
        lines.append("Шрифти: " + ", ".join(source_ref["fonts"][:6]))
    return "\n".join(lines)


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
    structure_text = _structure_block(source_ref)
    prompt = f"""Ти — AI-розробник, що створює ОРИГІНАЛЬНИЙ сайт, беручи за натхнення СТРУКТУРУ
іншого сайту. Відповідай українською (крім самого коду).

Нижче — ВИТЯГНУТА СТРУКТУРА (не сирий HTML) стороннього сайту: які блоки є,
як вони розташовані, які кольори й шрифти використано (визначені з реального
CSS сайту, якщо вдалось його завантажити). Це ЛИШЕ референс.

КАТЕГОРИЧНО ЗАБОРОНЕНО:
- копіювати текст, зображення, назви або логотипи з референсу;
- відтворювати сайт один-в-один.

ОБОВ'ЯЗКОВО:
- взяти лише загальну ідею компонування/стилю (структуру блоків, палітру, типографіку);
- повністю переробити під продукт користувача нижче: інші тексти, назви блоків,
  логотип (текстовий, якщо іншого нема), контент, тематику.

Структура референсу:
{structure_text}

Завдання користувача (продукт + побажання по стилю):
"{description}"

{_checklist_block(checklist or [])}
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.6)
    return _parse_site_response(data)


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
правильному порядку блоків, без дублювання і розривів. Визнач блоки, відступи,
шрифти, кольори, кнопки, картки, форми і загальний стиль, і відтвори сайт
максимально близько до зображень. Якщо якийсь фрагмент нечіткий — добери
найбільш логічне рішення, не зупиняй генерацію через це.

{extra}
{_checklist_block(checklist or [])}
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.4, images=images)
    return _parse_site_response(data)


async def generate_from_template(
    template_files: dict[str, str],
    description: str,
    checklist: list[str] | None = None,
    images: list[str] | None = None,
) -> dict | None:
    files_block = _build_files_block(template_files)
    extra = (
        f'Побажання користувача:\n"{description}"'
        if description else "Адаптуй шаблон під нейтральний, готовий до використання вигляд."
    )
    images_note = "Додано референс-скріншоти — врахуй їх стиль і контент при заміні." if images else ""
    prompt = f"""Ти — AI-розробник, що адаптує ГОТОВИЙ ШАБЛОН сайту під новий продукт,
НЕ переписуючи структуру й дизайн з нуля. Відповідай українською (крім коду).

Вихідний шаблон:
{files_block}

{extra}
{images_note}
{_checklist_block(checklist or [])}

Заміни тексти, назви, товари, контент під нову тематику. НЕ змінюй структуру
розмітки, CSS-класи й підключення скриптів, якщо це не потрібно для виконання
завдання користувача. Шляхи до вже наявних у шаблоні зображень лишай як є
(не вигадуй нові шляхи замість них). Якщо в шаблоні вже є форма замовлення —
не створюй другу, лише онови товар за замовчуванням. Якщо форми замовлення
немає — додай її.
{_RESPONSE_FORMAT_RULES}
{_ORDER_FORM_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.4, images=images)
    result = _parse_site_response(data)
    return result


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
    current_files: dict[str, str], product: dict, image_url: str,
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
- URL вже завантаженого фото (Cloudinary; використай ЯК Є, не вигадуй інший URL чи шлях): {image_url}

Завдання:
- Знайди на сторінці секцію/сітку товарів (якщо її ще немає — створи мінімальну
  секцію "Товари" перед формою замовлення).
- Додай ОДНУ нову картку товару з фото (<img src="{image_url}">), назвою, ціною
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


async def run_quality_check(files: dict[str, str]) -> list[str]:
    files_block = _build_files_block(files)
    prompt = f"""Ти — AI QA-інженер. Перевір якість цього сайту (HTML/CSS/JS).

Файли сайту:
{files_block}

{_QUALITY_CHECK_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.1)
    if not data or not isinstance(data.get("issues"), list):
        return []
    return [str(x).strip()[:200] for x in data["issues"] if str(x).strip()][:10]


async def fix_quality_issues(files: dict[str, str], issues: list[str]) -> dict | None:
    files_block = _build_files_block(files)
    items = "\n".join(f"- {i}" for i in issues)
    prompt = f"""Ти — AI-розробник, що виправляє знайдені технічні проблеми на сайті
(HTML/CSS/JS). Відповідай українською (крім коду).

Знайдені проблеми:
{items}

Поточні файли сайту:
{files_block}

Виправ усі перелічені проблеми, нічого іншого не змінюючи і не видаляючи
робочий функціонал (форму замовлення тощо). Поверни ПОВНИЙ оновлений набір файлів.
{_RESPONSE_FORMAT_RULES}"""
    data = await _generate_with_retry(prompt, temperature=0.2)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result