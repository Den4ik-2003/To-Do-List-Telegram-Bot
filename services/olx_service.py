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


async def fetch_listing_price(url: str) -> tuple[float, str] | None:
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

    soup = BeautifulSoup(html, "html.parser")

    price_el = soup.select_one('[data-testid="ad-price-container"]')
    if not price_el:
        price_el = soup.select_one('[data-testid="ad-price"]')

    text = price_el.get_text(" ", strip=True) if price_el else None
    if not text:
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            currency_meta = soup.find("meta", {"property": "product:price:currency"})
            try:
                return float(meta["content"]), (currency_meta["content"] if currency_meta else default_currency)
            except (ValueError, KeyError):
                pass

        title_tag = soup.find("title")
        logger.warning(
            "OLX listing: price not found. page_title=%r, snippet=%r",
            title_tag.get_text(strip=True) if title_tag else None,
            html[:500],
        )
        return None

    return _parse_price(text)


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