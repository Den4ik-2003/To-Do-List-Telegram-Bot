"""
ЗМІНЕНИЙ ФАЙЛ: services/olx_service.py

НОВЕ (пошук, search_listings):
- Запит із комами/крапками з комою ("покерний набір,покер,фішки") ділиться
  на окремі пошуки (до MAX_SUBQUERIES), результати об'єднуються без дублів.
  Раніше вся фраза з комами йшла в OLX як один рядок і давала 0 збігів.
- Пошук у кілька ступенів (кожен наступний — запасний):
    1) JSON-ендпоінт OLX /api/v1/offers/
    2) HTML: картки [data-cy="l-card"]
    3) HTML: картки за посиланнями "-ID....html"
    4) HTML: дані з window.__PRERENDERED_STATE__
- Якщо карток немає, а сторінка не схожа на «нічого не знайдено» (блок /
  антибот / нова розмітка), у лог іде діагностика, а функція повертає None
  (технічний збій), а не [] («0 оголошень»). Так збій парсера більше не
  маскується під «нічого вигідного не знайшов».

Решта функцій (деталі оголошення, фото, аудит) — без змін.
"""

import json
import logging
import re
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

logger = logging.getLogger("tasks_bot")

IMPERSONATE = "chrome124"

HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,pl;q=0.8,ru;q=0.7,en;q=0.6",
}

PRICE_RE = re.compile(r"([\d\s]+)(?:,\d+)?\s*(грн|UAH|zł|PLN|€|EUR|\$|USD)", re.IGNORECASE)
VIEWS_RE = re.compile(r"([\d\s]+)\s*(?:переглядів|перегляд|views|wyświetleń)", re.IGNORECASE)

CURRENCY_MAP = {
    "ГРН": "UAH", "UAH": "UAH",
    "ZŁ": "PLN", "PLN": "PLN",
    "€": "EUR", "EUR": "EUR",
    "$": "USD", "USD": "USD",
}

DOMAIN_CONFIG = {
    "olx.ua": {"list_path": "/uk/list/q-", "referer": "https://www.olx.ua/", "default_currency": "UAH"}
}

CONDITION_PARAM_MAP = {"used": "used", "new": "new"}

MAX_PHOTOS_FOR_AI = 10

API_URL = "https://www.{domain}/api/v1/offers/"
API_LIMIT = 40
MAX_SUBQUERIES = 3

_LISTING_HREF_RE = re.compile(r"-ID[a-zA-Z0-9]+\.html")

# Тексти сторінки, за якими видно, що OLX справді знайшов 0 оголошень
# (а не віддав заглушку / іншу розмітку).
NO_RESULTS_MARKERS = (
    "не знайшли", "нічого не знайдено", "не знайдено жодного",
    "nie znaleźliśmy", "we couldn't find",
)
BLOCK_MARKERS = ("captcha", "just a moment", "access denied", "cf-chl", "attention required")

# OLX сам додає цей reason до картки, коли розширений пошук НЕ дав жодного
# реального збігу і сайт підсовує випадкові оголошення з фіду, аби блок
# "схожі"/список не був порожнім. Такі картки — НЕ результати пошуку і їх
# треба відкидати, інакше видача виглядає як рандомний набір товарів
# (диван, чоботи, молоток замість реально схожих оголошень).
FALLBACK_REASON_MARKERS = (
    "extendedsearchnoresultslastresort",
)


def _parse_price(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    match = PRICE_RE.search(text)
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace("\xa0", "")
    currency = CURRENCY_MAP.get(match.group(2).upper(), "UAH")
    try:
        return float(number), currency
    except ValueError:
        return None


def _domain_headers(domain: str) -> dict:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])
    return {**HEADERS, "Referer": cfg["referer"]}


def _api_headers(domain: str) -> dict:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": HEADERS["Accept-Language"],
        "Referer": cfg["referer"],
    }


def _is_fallback_card(href: str) -> bool:
    """True, якщо OLX сам позначив цю картку як "останній резерв"
    (реального збігу за запитом немає)."""
    return any(marker in href for marker in FALLBACK_REASON_MARKERS)


def _best_srcset_url(srcset: str) -> str | None:
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.rsplit(" ", 1)
        url = bits[0].strip()
        width = 0
        if len(bits) == 2 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        candidates.append((width, url))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _extract_photos(soup: BeautifulSoup) -> tuple[list[str], int]:
    urls: list[str] = []
    seen: set[str] = set()

    gallery = (
        soup.select('[data-testid="image-gallery-container"] img')
        or soup.select('[data-testid="swiper-image"] img')
        or soup.select('[data-testid="ad-photo"] img')
    )

    for img in gallery:
        candidate = None
        if img.get("srcset"):
            candidate = _best_srcset_url(img["srcset"])
        if not candidate:
            candidate = img.get("data-src") or img.get("src")
        if not candidate or candidate.startswith("data:"):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)

    return urls[:MAX_PHOTOS_FOR_AI], len(urls)


def _parse_listing_html(html: str, default_currency: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {
        "price": None, "currency": default_currency, "title": None,
        "description": None, "location_text": None, "views": None,
        "photos": [], "photos_count": None, "params": [],
    }

    price_el = soup.select_one('[data-testid="ad-price-container"]') or soup.select_one('[data-testid="ad-price"]')
    price_text = price_el.get_text(" ", strip=True) if price_el else None
    if price_text:
        parsed = _parse_price(price_text)
        if parsed:
            result["price"], result["currency"] = parsed
    if result["price"] is None:
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            currency_meta = soup.find("meta", {"property": "product:price:currency"})
            try:
                result["price"] = float(meta["content"])
                result["currency"] = currency_meta["content"] if currency_meta else default_currency
            except (ValueError, KeyError):
                pass

    try:
        title_el = soup.select_one('[data-cy="ad_title"]') or soup.find("h1")
        if title_el:
            result["title"] = title_el.get_text(strip=True)
        elif soup.find("title"):
            result["title"] = soup.find("title").get_text(strip=True)
    except Exception:
        logger.exception("Не вдалося розпарсити назву оголошення")

    try:
        desc_el = soup.select_one('[data-cy="ad_description"]')
        if desc_el:
            result["description"] = desc_el.get_text(" ", strip=True)[:1500]
    except Exception:
        logger.exception("Не вдалося розпарсити опис оголошення")

    try:
        loc_el = soup.select_one('[data-testid="location-date"]')
        if loc_el:
            result["location_text"] = loc_el.get_text(" ", strip=True)
    except Exception:
        logger.exception("Не вдалося розпарсити локацію оголошення")

    try:
        views_el = soup.select_one('[data-testid="page-view-counter"]')
        views_text = views_el.get_text(" ", strip=True) if views_el else html
        views_match = VIEWS_RE.search(views_text)
        if views_match:
            result["views"] = int(views_match.group(1).replace(" ", "").replace("\xa0", ""))
    except Exception:
        logger.exception("Не вдалося розпарсити кількість переглядів")

    try:
        photos, total = _extract_photos(soup)
        result["photos"] = photos
        result["photos_count"] = total
    except Exception:
        logger.exception("Не вдалося розпарсити фото оголошення")

    try:
        params_container = soup.select_one('[data-testid="ad-parameters-container"]')
        if params_container:
            for li in params_container.find_all("li"):
                text = li.get_text(" ", strip=True)
                if text:
                    result["params"].append(text)
    except Exception:
        logger.exception("Не вдалося розпарсити характеристики оголошення")

    return result


async def fetch_listing_details(url: str) -> dict | None:
    domain = "olx.pl" if "olx.pl" in url else "olx.ua"
    headers = _domain_headers(domain)
    default_currency = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])["default_currency"]

    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=headers) as session:
            resp = await session.get(url, timeout=20, allow_redirects=True)
            final_url = str(resp.url)
            if resp.status_code != 200:
                logger.warning("OLX listing fetch status=%s for %s (final_url=%s)", resp.status_code, url, final_url)
                return None
            html = resp.text
    except Exception:
        logger.exception("OLX listing fetch failed for %s", url)
        return None

    logger.info("OLX listing fetch OK, final_url=%s, html_len=%s", final_url, len(html))

    details = _parse_listing_html(html, default_currency)
    if details["price"] is None:
        title_tag_text = details.get("title")
        logger.warning("OLX listing: price not found. page_title=%r, snippet=%r", title_tag_text, html[:500])
        return None
    return details


async def fetch_listing_price(url: str) -> tuple[float, str] | None:
    details = await fetch_listing_details(url)
    if not details or details["price"] is None:
        return None
    return details["price"], details["currency"]


def _guess_image_mime(url: str) -> str:
    """Для 🔍 Аудит мого оголошення — грубе визначення MIME за розширенням
    у URL, щоб коректно сформувати data:URI для vision-запиту. OLX CDN інколи
    не віддає розширення в чистому вигляді (query-параметри після нього),
    тому спочатку відрізаємо все після "?"."""
    lower = url.split("?")[0].lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


async def fetch_image_bytes(url: str) -> tuple[bytes, str] | None:
    """Для 🔍 Аудит мого оголошення — завантажує сирі байти фото з OLX CDN
    для подальшого vision-аналізу. Повертає (bytes, mime) або None."""
    try:
        async with AsyncSession(impersonate=IMPERSONATE, timeout=15) as session:
            resp = await session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning("OLX image fetch status=%s for %s", resp.status_code, url)
                return None
            return resp.content, _guess_image_mime(url)
    except Exception:
        logger.exception("OLX image fetch failed for %s", url)
        return None


def _build_search_url(
    domain: str,
    title_query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
    condition: str | None = None,
) -> str:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])
    slug = title_query.strip().replace(" ", "-")
    query = quote(slug, safe="-")
    base = f"https://www.{domain}{cfg['list_path']}{query}/"
    params = []
    if max_price:
        params.append(f"search[filter_float_price:to]={int(max_price)}")
    if location:
        params.append(f"search[dist]={radius_km}")
    if condition and condition in CONDITION_PARAM_MAP:
        params.append(f"search[filter_enum_state][0]={CONDITION_PARAM_MAP[condition]}")
    if params:
        base += "?" + "&".join(params)
    return base


# =========================================================
# НОВЕ: пошук оголошень (багатоступеневий)
# =========================================================

def _split_queries(title_query: str) -> list[str]:
    """'покерний набір, покер, фішки' -> ['покерний набір', 'покер', 'фішки'].
    Дублі (без урахування регістру) відкидаються, максимум MAX_SUBQUERIES."""
    parts = [p.strip() for p in re.split(r"[,;\n]", title_query or "") if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[:MAX_SUBQUERIES] or [(title_query or "").strip()]


def _absolute_url(href: str, domain: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"https://www.{domain}" + href
    return href


def _listing_id(url: str, fallback=None) -> str:
    m = re.search(r"-ID([a-zA-Z0-9]+)\.html", url)
    if m:
        return m.group(1)
    return str(fallback) if fallback not in (None, "") else url


def _offer_price(offer: dict) -> tuple[float, str | None] | None:
    """Ціна з offer: або params[key=price].value (JSON API), або
    price.regularPrice (prerendered state), або просто число."""
    for p in offer.get("params") or []:
        if isinstance(p, dict) and p.get("key") == "price":
            val = p.get("value") or {}
            if isinstance(val, dict) and val.get("value") is not None:
                try:
                    return float(val["value"]), val.get("currency")
                except (TypeError, ValueError):
                    pass

    price = offer.get("price")
    if isinstance(price, dict):
        reg = price.get("regularPrice") or price
        if isinstance(reg, dict) and reg.get("value") is not None:
            try:
                return float(reg["value"]), reg.get("currencyCode") or reg.get("currency")
            except (TypeError, ValueError):
                pass
    elif isinstance(price, (int, float)):
        return float(price), None
    return None


def _offer_location(offer: dict) -> str:
    loc = offer.get("location") or {}
    if not isinstance(loc, dict):
        return ""
    city = loc.get("city")
    if isinstance(city, dict) and city.get("name"):
        return str(city["name"])
    return str(loc.get("cityName") or loc.get("pathName") or "")


def _offer_to_result(offer: dict, domain: str, cfg: dict) -> dict | None:
    """Приводить оголошення з JSON (API або prerendered state) до того
    самого формату, що й HTML-картка."""
    if not isinstance(offer, dict):
        return None
    url = offer.get("url")
    if not url:
        return None
    url = _absolute_url(str(url), domain)
    if _is_fallback_card(url):
        return None

    price_info = _offer_price(offer)
    price = price_info[0] if price_info else None
    raw_currency = price_info[1] if price_info else None
    currency = CURRENCY_MAP.get(str(raw_currency or "").upper(), cfg["default_currency"])

    return {
        "id": _listing_id(url, offer.get("id")),
        "url": url,
        "title": str(offer.get("title") or "Без назви").strip(),
        "price": price,
        "currency": currency,
        "location_text": _offer_location(offer),
    }


async def _search_via_api(query: str, max_price: float | None, condition: str | None,
                          domain: str, cfg: dict) -> list[dict] | None:
    """Ступінь 1: JSON-ендпоінт OLX. None — API не спрацював (пробуємо HTML),
    [] — API відповів, але оголошень нема."""
    params: dict = {"offset": 0, "limit": API_LIMIT, "query": query}
    if max_price:
        params["filter_float_price:to"] = int(max_price)
    if condition and condition in CONDITION_PARAM_MAP:
        params["filter_enum_state[0]"] = CONDITION_PARAM_MAP[condition]

    url = API_URL.format(domain=domain)
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=_api_headers(domain)) as session:
            resp = await session.get(url, params=params, timeout=20)
            if resp.status_code != 200:
                logger.warning("OLX API search status=%s query=%r", resp.status_code, query)
                return None
            data = resp.json()
    except Exception:
        logger.exception("OLX API search failed query=%r", query)
        return None

    offers = data.get("data") if isinstance(data, dict) else None
    if not isinstance(offers, list):
        logger.warning("OLX API search: несподівана структура відповіді query=%r", query)
        return None

    results = []
    for offer in offers:
        item = _offer_to_result(offer, domain, cfg)
        if item:
            results.append(item)

    logger.info("OLX API search OK query=%r offers=%s parsed=%s", query, len(offers), len(results))
    return results


def _parse_cards(soup: BeautifulSoup, domain: str, cfg: dict) -> tuple[list[dict], int]:
    """Ступінь 2: класичні картки [data-cy="l-card"]."""
    cards = soup.select('[data-cy="l-card"]')
    results = []
    fallback_skipped = 0
    for card in cards:
        link_el = card.select_one("a")
        href = link_el.get("href") if link_el else None
        if not href:
            continue
        href = _absolute_url(href, domain)

        if _is_fallback_card(href):
            fallback_skipped += 1
            continue

        title_el = card.select_one('[data-cy="ad-card-title"] h4') or card.select_one("h4") or card.select_one("h6")
        title = title_el.get_text(strip=True) if title_el else "Без назви"

        price_el = card.select_one('[data-testid="ad-price"]')
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        parsed_price = _parse_price(price_text)

        location_el = card.select_one('[data-testid="location-date"]')
        location_text = location_el.get_text(strip=True) if location_el else ""

        results.append({
            "id": _listing_id(href),
            "url": href,
            "title": title,
            "price": parsed_price[0] if parsed_price else None,
            "currency": parsed_price[1] if parsed_price else cfg["default_currency"],
            "location_text": location_text,
        })
    return results, fallback_skipped


def _cards_by_links(soup: BeautifulSoup, domain: str, cfg: dict) -> list[dict]:
    """Ступінь 3: якщо OLX перейменував data-cy — шукаємо посилання на
    оголошення (…-IDxxxx.html) і збираємо дані з найближчого контейнера."""
    results = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _LISTING_HREF_RE.search(href):
            continue
        href = _absolute_url(href, domain)
        if href in seen or _is_fallback_card(href):
            continue
        seen.add(href)

        container = a
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            if len(container.get_text(" ", strip=True)) > 60:
                break
        text = container.get_text(" ", strip=True)
        title_el = container.select_one("h4, h6, h3")
        title = title_el.get_text(strip=True) if title_el else (a.get_text(strip=True) or "Без назви")
        parsed_price = _parse_price(text)

        results.append({
            "id": _listing_id(href),
            "url": href,
            "title": title,
            "price": parsed_price[0] if parsed_price else None,
            "currency": parsed_price[1] if parsed_price else cfg["default_currency"],
            "location_text": "",
        })
    return results


def _find_ads(node, depth: int = 0):
    """Шукає в JSON-стані сторінки непорожній масив ads з оголошеннями."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        ads = node.get("ads")
        if isinstance(ads, list) and ads and isinstance(ads[0], dict) and ads[0].get("url"):
            return ads
        for v in node.values():
            found = _find_ads(v, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_ads(item, depth + 1)
            if found:
                return found
    return None


def _parse_prerendered_ads(html: str, domain: str, cfg: dict) -> list[dict]:
    """Ступінь 4: дані з window.__PRERENDERED_STATE__ (JSON-рядок у HTML)."""
    idx = html.find("__PRERENDERED_STATE__")
    if idx == -1:
        return []
    q = html.find('"', idx)
    if q == -1:
        return []
    try:
        inner, _ = json.JSONDecoder().raw_decode(html[q:])
        state = json.loads(inner) if isinstance(inner, str) else inner
    except (ValueError, TypeError):
        return []

    ads = _find_ads(state)
    if not ads:
        return []
    results = []
    for ad in ads:
        item = _offer_to_result(ad, domain, cfg)
        if item:
            results.append(item)
    return results


def _diagnose_html(html: str) -> dict:
    low = html.lower()
    title_m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return {
        "len": len(html),
        "title": (title_m.group(1).strip()[:100] if title_m else None),
        "l_card": low.count('data-cy="l-card"'),
        "prerendered": "__prerendered_state__" in low,
        "block_markers": [m for m in BLOCK_MARKERS if m in low],
    }


async def _search_via_html(query: str, max_price: float | None, location: str, radius_km: int,
                           domain: str, condition: str | None, cfg: dict) -> list[dict] | None:
    """Ступені 2-4. None — сторінка не розпізнана (блок / нова розмітка),
    [] — OLX справді нічого не знайшов."""
    url = _build_search_url(domain, query, max_price, location, radius_km, condition)
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=_domain_headers(domain)) as session:
            resp = await session.get(url, timeout=20, allow_redirects=True)
            if resp.status_code != 200:
                logger.warning("OLX search fetch status=%s for %s", resp.status_code, url)
                return None
            html = resp.text
    except Exception:
        logger.exception("OLX search fetch failed for %s", url)
        return None

    soup = BeautifulSoup(html, "html.parser")
    results, fallback_skipped = _parse_cards(soup, domain, cfg)
    cards_found = len(soup.select('[data-cy="l-card"]'))
    logger.info("OLX search OK url=%s cards_found=%s", url, cards_found)

    if fallback_skipped:
        logger.info(
            "OLX search url=%s: відкинуто %s fallback-карток (extendedsearchnoresultslastresort) — "
            "реальних збігів за запитом %r не знайдено",
            url, fallback_skipped, query,
        )

    if results:
        return results
    if cards_found:
        return []  # були лише fallback-картки — реальних збігів немає

    # data-cy="l-card" не знайдено — пробуємо запасні способи
    results = _cards_by_links(soup, domain, cfg)
    if results:
        logger.info("OLX search: картки знайдено за посиланнями (l-card відсутні) query=%r n=%s", query, len(results))
        return results

    results = _parse_prerendered_ads(html, domain, cfg)
    if results:
        logger.info("OLX search: оголошення взято з __PRERENDERED_STATE__ query=%r n=%s", query, len(results))
        return results

    low = html.lower()
    if any(marker in low for marker in NO_RESULTS_MARKERS):
        logger.info("OLX search: 0 оголошень (сторінка підтверджує «нічого не знайдено») query=%r", query)
        return []

    logger.warning(
        "OLX search: 0 оголошень, але сторінка не схожа на «нічого не знайдено» — "
        "схоже на блокування або нову розмітку. query=%r url=%s diag=%s snippet=%r",
        query, url, _diagnose_html(html), html[:300],
    )
    return None


async def _search_single(query: str, max_price: float | None, location: str, radius_km: int,
                         domain: str, condition: str | None) -> list[dict] | None:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])

    api_result = await _search_via_api(query, max_price, condition, domain, cfg)
    if api_result:
        return api_result

    html_result = await _search_via_html(query, max_price, location, radius_km, domain, condition, cfg)
    if html_result:
        return html_result

    if api_result == []:
        return []  # API відповів «порожньо», HTML нічого не додав
    return html_result  # [] або None


async def search_listings(
    title_query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
    domain: str = "olx.ua",
    condition: str | None = None,
) -> list[dict] | None:
    """Повертає список оголошень, [] якщо OLX нічого не знайшов, або None
    при технічному збої (усі запити провалились). Запит із комами
    розбивається на кілька пошуків, результати об'єднуються."""
    queries = _split_queries(title_query)
    merged: dict[str, dict] = {}
    failures = 0

    for q in queries:
        items = await _search_single(q, max_price, location, radius_km, domain, condition)
        if items is None:
            failures += 1
            continue
        for item in items:
            merged.setdefault(item["id"], item)

    if failures == len(queries):
        logger.warning("OLX search: усі %s запит(ів) не вдалися, query=%r", len(queries), title_query)
        return None

    logger.info(
        "OLX search підсумок: запитів=%s збоїв=%s унікальних оголошень=%s (query=%r)",
        len(queries), failures, len(merged), title_query,
    )
    return list(merged.values())


def sort_by_price(results: list[dict], ascending: bool = True) -> list[dict]:
    priced = [r for r in results if r.get("price") is not None]
    unpriced = [r for r in results if r.get("price") is None]
    priced.sort(key=lambda r: r["price"], reverse=not ascending)
    return priced + unpriced