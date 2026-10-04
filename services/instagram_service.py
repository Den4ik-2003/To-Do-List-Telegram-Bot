import asyncio
import json
import logging
import os
import re

import aiohttp
from bs4 import BeautifulSoup

from services import website_builder_service as wbs

logger = logging.getLogger("tasks_bot")

GRAPH_VERSION = "v21.0"
GRAPH_TOKEN = os.environ.get("INSTAGRAM_GRAPH_TOKEN", "")
GRAPH_USER_ID = os.environ.get("INSTAGRAM_GRAPH_USER_ID", "")

MAX_IMAGE_BYTES = 6_000_000
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_IG_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_RESERVED = {
    "p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct",
    "about", "developer", "legal", "web", "directory",
}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+38)?[\s-]?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)|\+\d{10,14}")
_TG_LINK_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})", re.IGNORECASE)
_TG_WORD_RE = re.compile(r"(?:telegram|tg|телеграм|тг)\W{0,3}@?([A-Za-z0-9_]{4,32})", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BIO_RE = re.compile(r"Instagram:\s*[\"“«](.+)[\"”»]\s*$", re.DOTALL)

_FAIL_TEXT = "❌ Не вдалося отримати дані Instagram-профілю."

_FORMAT_RULES = """
Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown-розмітки, у форматі:
{
  "summary": "короткий опис українською, що це за сайт і яку структуру він має",
  "site_name": "коротка kebab-case назва латиницею",
  "commit_message": "короткий commit message англійською, у стилі conventional commits",
  "files": {
    "index.html": "повний вміст файлу",
    "style.css": "повний вміст файлу",
    "script.js": "повний вміст файлу"
  }
}
Правила для файлів: чистий HTML/CSS/JS без збірки, одразу працює на статичному
хостингу. Повністю адаптивний (мобільна версія обов'язкова), обов'язково
<meta name="viewport" content="width=device-width, initial-scale=1">. Максимум 6 файлів.
Стилі окремо в style.css, скрипти окремо в script.js. Семантичний HTML.
"""


class InstagramError(Exception):
    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(code)


def parse_instagram_url(text: str) -> str | None:
    m = _IG_URL_RE.match((text or "").strip())
    if not m:
        return None
    username = m.group(1).lower()
    if username in _RESERVED:
        return None
    return username


def site_slug(username: str) -> str:
    return wbs.slugify(f"instagram-{username}", fallback="instagram-shop")


def extract_contacts(profile: dict) -> dict:
    parts = [profile.get("bio") or "", profile.get("website") or ""]
    parts += [p.get("caption") or "" for p in profile.get("posts") or []]
    text = "\n".join(parts)

    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))[:3]

    phones: list[str] = []
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if m.group(0).strip().startswith("+"):
            normalized = "+" + digits
        elif len(digits) == 10:
            normalized = "+38" + digits
        else:
            normalized = "+" + digits
        if normalized not in phones:
            phones.append(normalized)
    phones = phones[:3]

    telegram: list[str] = []
    for m in list(_TG_LINK_RE.finditer(text)) + list(_TG_WORD_RE.finditer(text)):
        handle = m.group(1)
        if handle.lower() not in {t.lower() for t in telegram}:
            telegram.append(handle)
    telegram = telegram[:2]

    links: list[str] = []
    for m in _URL_RE.finditer(text):
        link = m.group(0).rstrip(".,;)")
        low = link.lower()
        if "instagram.com" in low or "t.me/" in low:
            continue
        if link not in links:
            links.append(link)
    website = (profile.get("website") or "").strip()
    if website.startswith("http") and website not in links and "instagram.com" not in website.lower():
        links.insert(0, website)
    links = links[:4]

    return {"email": emails, "phones": phones, "telegram": telegram, "links": links}


def profile_is_thin(profile: dict) -> bool:
    contacts = profile.get("contacts") or {}
    has_contacts = any(contacts.get(k) for k in ("email", "phones", "telegram", "links"))
    return not profile.get("bio") and not profile.get("posts") and not has_contacts


async def _fetch_graph(username: str) -> dict | None:
    if not (GRAPH_TOKEN and GRAPH_USER_ID):
        return None
    fields = (
        f"business_discovery.username({username})"
        "{username,name,biography,website,profile_picture_url,media_count,"
        "media.limit(12){caption,media_type,media_url,thumbnail_url,permalink}}"
    )
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{GRAPH_USER_ID}"
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url, params={"fields": fields, "access_token": GRAPH_TOKEN}) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    logger.info("Instagram Graph API status=%s for %s: %s", resp.status, username, str(data)[:300])
                    return None
    except Exception:
        logger.exception("Instagram Graph API request failed for %s", username)
        return None

    bd = data.get("business_discovery") if isinstance(data, dict) else None
    if not isinstance(bd, dict):
        return None

    posts = []
    for m in ((bd.get("media") or {}).get("data") or []):
        mtype = m.get("media_type")
        image = m.get("media_url") if mtype in ("IMAGE", "CAROUSEL_ALBUM") else m.get("thumbnail_url")
        if image:
            posts.append({
                "image_url": image,
                "caption": (m.get("caption") or "")[:300],
                "permalink": m.get("permalink"),
            })

    return {
        "username": (bd.get("username") or username).lower(),
        "url": f"https://www.instagram.com/{username}/",
        "full_name": bd.get("name") or "",
        "bio": bd.get("biography") or "",
        "website": bd.get("website") or "",
        "profile_pic_url": bd.get("profile_picture_url") or "",
        "posts": posts,
        "source": "graph_api",
    }


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


async def _fetch_public(username: str) -> dict:
    url = f"https://www.instagram.com/{username}/"
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"},
                allow_redirects=True,
            ) as resp:
                status = resp.status
                final_url = str(resp.url)
                html_text = await resp.text(errors="ignore") if status == 200 else ""
    except Exception:
        logger.exception("Instagram public fetch failed for %s", username)
        raise InstagramError("unavailable", _FAIL_TEXT)

    if status == 404:
        raise InstagramError("not_found", f"{_FAIL_TEXT}\n\nПрофіль не знайдено. Перевір посилання.")
    if status != 200:
        raise InstagramError("unavailable", _FAIL_TEXT)

    low = html_text.lower()
    if "this account is private" in low or "цей акаунт приватний" in low:
        raise InstagramError("private", f"{_FAIL_TEXT}\n\nПрофіль приватний, дані недоступні.")
    if "/accounts/login" in final_url:
        raise InstagramError("unavailable", f"{_FAIL_TEXT}\n\nInstagram вимагає вхід для перегляду цього профілю.")

    soup = BeautifulSoup(html_text, "html.parser")
    title = _meta(soup, "og:title")
    description = _meta(soup, "og:description") or _meta(soup, "description")
    image = _meta(soup, "og:image")

    if not title or title.strip().lower() == "instagram":
        raise InstagramError("unavailable", f"{_FAIL_TEXT}\n\nInstagram не віддав публічні дані без входу.")

    full_name = ""
    m = re.match(r"^(.*?)\s*\(@", title)
    if m:
        full_name = m.group(1).strip()
    elif "•" in title:
        full_name = title.split("•", 1)[0].strip()

    bio = ""
    bm = _BIO_RE.search(description)
    if bm:
        bio = bm.group(1).strip()

    return {
        "username": username,
        "url": url,
        "full_name": full_name,
        "bio": bio,
        "website": "",
        "profile_pic_url": image,
        "posts": [],
        "source": "public_meta",
    }


async def fetch_profile(username: str) -> dict:
    profile = await _fetch_graph(username)
    if profile is None:
        profile = await _fetch_public(username)
    profile["contacts"] = extract_contacts(profile)
    return profile


async def download_image(url: str) -> bytes | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status != 200:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not ctype.startswith("image/"):
                    return None
                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
                if not data or len(data) > MAX_IMAGE_BYTES:
                    return None
                return data
    except Exception:
        logger.info("Instagram image download failed: %s", url[:120])
        return None


def _build_prompt(profile: dict, extra: str, refs: list[dict]) -> str:
    data = {
        "instagram_username": profile["username"],
        "instagram_url": profile["url"],
        "name": profile.get("full_name") or None,
        "bio": profile.get("bio") or None,
        "website": profile.get("website") or None,
        "contacts": profile.get("contacts") or {},
        "post_captions": [p["caption"] for p in profile.get("posts") or [] if p.get("caption")][:12],
    }
    images = [
        {
            "file": r["ref"],
            "kind": r["kind"],
            "caption": r.get("caption") or "",
            "post_link": r.get("link"),
        }
        for r in refs
    ]
    extra_block = (
        f"Додаткова інформація від власника (факти, їм можна довіряти: товари, ціни, контакти):\n{extra}"
        if extra else "Додаткової інформації від власника немає."
    )
    images_block = (
        json.dumps(images, ensure_ascii=False, indent=1)
        if images else "Реальних зображень немає."
    )
    return f"""Ти — AI-розробник і дизайнер, що створює сайт для КОНКРЕТНОГО магазину/бізнесу на основі його Instagram-профілю.
Відповідай українською (крім коду). Мова текстів сайту — мова профілю, якщо не зрозуміло — українська.

Дані профілю (єдине джерело фактів):
{json.dumps(data, ensure_ascii=False, indent=1)}

{extra_block}

Доступні реальні зображення (використовуй ТІЛЬКИ ці значення "file" у src, точно як є):
{images_block}

Завдання:
1. Визнач тип бізнесу, нішу, стиль, настрій, tone of voice за назвою, описом, підписами й зображеннями.
2. Сам обери структуру сайту під цей бізнес, а не універсальну. Орієнтири:
   - кросівки/взуття: Hero, Каталог, Популярні моделі, Переваги, Про магазин, Instagram, Контакти, CTA;
   - годинники/аксесуари: Hero, Колекція, Категорії, Популярні моделі, Переваги, Про магазин, Instagram, Контакти;
   - одяг: Hero, Новинки, Каталог, Категорії, Про бренд/магазин, Instagram, Контакти;
   - інший бізнес: обери релевантні блоки сам.
3. Адаптуй дизайн: палітру (з зображень і стилю профілю), шрифти (Google Fonts через <link> дозволено, обов'язково з fallback), типографіку, картки, кнопки, layout. Мінімалістичний профіль — мінімалістичний сайт, luxury — преміум, streetwear — streetwear, жіночий — fashion. Сайт має виглядати як сайт саме цього магазину.
4. Фото профілю використовуй як логотип/аватар або в блоці «Про магазин». Фото постів — для каталогу/галереї/Hero; якщо є post_link, галерея може вести на цей пост.

СУВОРО ЗАБОРОНЕНО вигадувати: товари, ціни, характеристики, відгуки, рейтинги, статистику, адреси, телефони, email, посилання, роки роботи, акції.
- Товари показуй лише якщо вони є в даних (фото постів, підписи, додаткова інформація). Назву й короткий опис бери з підпису або додаткової інформації, без вигаданих деталей.
- Ціна невідома — напиши «Ціна за запитом» або не показуй ціну.
- Контакти додавай тільки ті, що є в contacts, додатковій інформації чи профілі. Немає — не створюй блок для них.
- Мало даних — зроби сайт лише з доступного (Hero, про магазин, Instagram, контакти), без вигаданого наповнення.

Зображення:
- Не використовуй стокові фото та випадкові картинки (picsum, unsplash тощо) і не вигадуй шляхи до файлів.
- Кожен <img> має alt і onerror, що ховає картинку або підставляє градієнтний placeholder, щоб сайт не ламався.
- Декоративні фони роби CSS-градієнтами/кольорами. Якщо реальних зображень немає — елегантний типографічний дизайн.

Instagram і CTA:
- Обов'язково блок «Instagram» з @{profile['username']} і кнопкою «Перейти в Instagram» на {profile['url']}.
- CTA за типом бізнесу: «Замовити» (відкриває форму замовлення), «Написати в Instagram», «Переглянути Instagram».
- У контактах: Instagram, а також Telegram, телефон, email, сайт — лише якщо вони є в даних.
{_FORMAT_RULES}
{wbs._ORDER_FORM_RULES}"""


async def generate_site(profile: dict, extra: str, refs: list[dict], vision: list[str]) -> dict | None:
    prompt = _build_prompt(profile, extra, refs)
    data = await wbs._generate_with_retry(prompt, temperature=0.6, images=vision or None)
    if not data and vision:
        data = await wbs._generate_with_retry(prompt, temperature=0.6)
    result = wbs._parse_site_response(data)
    if result:
        result["site_name"] = site_slug(profile["username"])
    return result