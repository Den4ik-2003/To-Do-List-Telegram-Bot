import logging
import re

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

logger = logging.getLogger("tasks_bot")

# ВАЖЛИВО: aiohttp з "браузерними" заголовками все одно отримував 403 від OLX.
# Причина — не заголовки, а TLS-відбиток з'єднання: захист розпізнає
# non-браузерні HTTP-клієнти за тим, ЯК вони встановлюють TLS (порядок
# cipher suites, ALPN, HTTP/2-параметри), а не лише за заголовками.
# curl_cffi вміє відтворювати РЕАЛЬНИЙ TLS-відбиток конкретної версії Chrome
# (impersonate="chrome124") — це і обходить цей тип 403.
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
    "olx.ua": {"list_path": "/uk/list/q-", "referer": "https://www.olx.ua/", "default_currency": "UAH"},
    "olx.pl": {"list_path": "/oferty/q-", "referer": "https://www.olx.pl/", "default_currency": "PLN"},
}


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


def _parse_listing_html(html: str, default_currency: str) -> dict:
    """
    Витягує з HTML оголошення все, що потрібно для оцінки вигідності
    перепродажу: ціну, назву, опис, локацію, перегляди, кількість фото
    та список характеристик (стан, бренд тощо, якщо вказані продавцем).
    Кожне поле окремо обгорнуте в try, щоб відсутність одного елемента
    (наприклад лічильника переглядів на старій версії сторінки) не ламала
    весь парсинг решти даних.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {
        "price": None, "currency": default_currency, "title": None,
        "description": None, "location_text": None, "views": None,
        "photos_count": None, "params": [],
    }

    # --- Ціна ---
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

    # --- Назва ---
    try:
        title_el = soup.select_one('[data-cy="ad_title"]') or soup.find("h1")
        if title_el:
            result["title"] = title_el.get_text(strip=True)
        elif soup.find("title"):
            result["title"] = soup.find("title").get_text(strip=True)
    except Exception:
        logger.exception("Не вдалося розпарсити назву оголошення")

    # --- Опис ---
    try:
        desc_el = soup.select_one('[data-cy="ad_description"]')
        if desc_el:
            result["description"] = desc_el.get_text(" ", strip=True)[:1500]
    except Exception:
        logger.exception("Не вдалося розпарсити опис оголошення")

    # --- Локація/дата ---
    try:
        loc_el = soup.select_one('[data-testid="location-date"]')
        if loc_el:
            result["location_text"] = loc_el.get_text(" ", strip=True)
    except Exception:
        logger.exception("Не вдалося розпарсити локацію оголошення")

    # --- Перегляди ---
    try:
        views_el = soup.select_one('[data-testid="page-view-counter"]')
        views_text = views_el.get_text(" ", strip=True) if views_el else html
        views_match = VIEWS_RE.search(views_text)
        if views_match:
            result["views"] = int(views_match.group(1).replace(" ", "").replace("\xa0", ""))
    except Exception:
        logger.exception("Не вдалося розпарсити кількість переглядів")

    # --- Кількість фото ---
    try:
        gallery = soup.select('[data-testid="image-gallery-container"] img') or soup.select('[data-testid="swiper-image"]')
        if gallery:
            result["photos_count"] = len(gallery)
    except Exception:
        logger.exception("Не вдалося порахувати кількість фото")

    # --- Характеристики (стан, бренд, рік тощо — якщо вказані продавцем) ---
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
    """
    Повне зчитування оголошення: ціна, назва, опис, локація, перегляди,
    кількість фото, характеристики. Використовується і для стеження за
    ціною, і для AI-оцінки вигідності перепродажу.
    """
    domain = "olx.pl" if "olx.pl" in url else "olx.ua"
    headers = _domain_headers(domain)
    default_currency = DOMAIN_CONFIG[domain]["default_currency"]

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
    """Легка версія для періодичної джоби перевірки ціни (без опису/фото/параметрів)."""
    details = await fetch_listing_details(url)
    if not details or details["price"] is None:
        return None
    return details["price"], details["currency"]


def _build_search_url(domain: str, title_query: str, max_price: float | None, location: str, radius_km: int) -> str:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])
    query = title_query.strip().replace(" ", "-")
    base = f"https://www.{domain}{cfg['list_path']}{query}/"
    params = []
    if max_price:
        params.append(f"search[filter_float_price:to]={int(max_price)}")
    if location:
        params.append(f"search[dist]={radius_km}")
    if params:
        base += "?" + "&".join(params)
    return base


async def search_listings(
    title_query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
    domain: str = "olx.ua",
) -> list[dict]:
    cfg = DOMAIN_CONFIG.get(domain, DOMAIN_CONFIG["olx.ua"])
    url = _build_search_url(domain, title_query, max_price, location, radius_km)
    headers = _domain_headers(domain)

    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=headers) as session:
            resp = await session.get(url, timeout=20, allow_redirects=True)
            if resp.status_code != 200:
                logger.warning("OLX search fetch status=%s for %s", resp.status_code, url)
                return []
            html = resp.text
    except Exception:
        logger.exception("OLX search fetch failed for %s", url)
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-cy="l-card"]')

    results = []
    for card in cards:
        link_el = card.select_one("a")
        href = link_el.get("href") if link_el else None
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://www.{domain}" + href

        listing_id_match = re.search(r"-ID([a-zA-Z0-9]+)\.html", href)
        listing_id = listing_id_match.group(1) if listing_id_match else href

        title_el = card.select_one('[data-cy="ad-card-title"] h4') or card.select_one("h4") or card.select_one("h6")
        title = title_el.get_text(strip=True) if title_el else "Без назви"

        price_el = card.select_one('[data-testid="ad-price"]')
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        parsed_price = _parse_price(price_text)

        location_el = card.select_one('[data-testid="location-date"]')
        location_text = location_el.get_text(strip=True) if location_el else ""

        results.append({
            "id": listing_id,
            "url": href,
            "title": title,
            "price": parsed_price[0] if parsed_price else None,
            "currency": parsed_price[1] if parsed_price else cfg["default_currency"],
            "location_text": location_text,
        })

    return results