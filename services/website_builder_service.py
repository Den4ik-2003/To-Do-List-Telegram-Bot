"""
НОВИЙ ФАЙЛ: services/website_builder_service.py

Логіка фічі "🌐 AI Website Builder". Нічого не дублює:
- GitHub-токен і запис у репозиторій — services/github_api.py
  (deploy_files — той самий шлях, що й Deploy ZIP / AI Developer;
  create_repo/repo_exists — НОВІ функції, їх треба додати в github_api.py,
  див. окремий snippet).
- Netlify — services/netlify_service.py (новий, ізольований файл).
- AI-виклики — services/ai_service.generate_json (той самий клієнт, що
  й у ai_developer_service/planner_service).

Ключове архітектурне рішення: AI ЗАВЖДИ повертає ПОВНИЙ набір файлів
сайту (а не diff/патч). Це свідомо спрощує деплой — і на GitHub, і на
Netlify щоразу йде весь актуальний набір файлів, без злиття "старе +
нове". Для лендінгів на кілька файлів (html/css/js) це дешево і надійно.
"""

import logging
import re

import aiohttp

from services import ai_service

logger = logging.getLogger("tasks_bot")

MAX_SOURCE_HTML_CHARS = 12000   # скільки символів HTML-джерела дати AI як референс стилю
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
    """Валідує та санітизує JSON-відповідь AI у форматі:
    {"summary": str, "site_name": str, "commit_message": str,
     "files": {"index.html": "...", "style.css": "...", ...}}"""
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
        return None  # без index.html сайт неможливо задеплоїти як лендінг

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


async def fetch_source_reference(url: str) -> dict | None:
    """Тягне HTML стороннього сайту як РЕФЕРЕНС стилю/структури для AI.
    Це НЕ контент майбутнього сайту — далі AI явно проінструктовано не
    копіювати текст/зображення/код звідти, а лише орієнтуватись на
    загальну структуру (шапка/hero/сітка товарів/футер тощо)."""
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
{_RESPONSE_FORMAT_RULES}"""
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
```
{source_ref['html_excerpt']}
```

Завдання користувача (продукт + побажання по стилю):
"{description}"

{_RESPONSE_FORMAT_RULES}"""
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
сайту (і змінені, і незмінені), а не тільки ті, що торкнулись правки.
{_RESPONSE_FORMAT_RULES}"""
    data = await ai_service.generate_json(prompt, temperature=0.4)
    result = _parse_site_response(data)
    if result and not result.get("site_name"):
        result["site_name"] = None
    return result