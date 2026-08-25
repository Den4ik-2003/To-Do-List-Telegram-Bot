import logging
import re

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

logger = logging.getLogger("tasks_bot")

# ВАЖЛИВО: раніше тут використовувався aiohttp з "браузерними" заголовками
# (User-Agent, Accept, Referer тощо), але OLX все одно повертав 403.
# Причина — не заголовки, а TLS-відбиток з'єднання: Cloudflare/Akamai-подібний
# захист розпізнає non-браузерні HTTP-клієнти за тим, ЯК вони встановлюють
# TLS (порядок cipher suites, ALPN, HTTP/2-параметри), а не лише за
# заголовками. aiohttp завжди має "не-браузерний" відбиток, скільки
# заголовків не додавай.
#
# curl_cffi вміє відтворювати РЕАЛЬНИЙ TLS-відбиток конкретної версії Chrome
# (impersonate="chrome124") — це і обходить саме цей тип 403.
IMPERSONATE = "chrome124"

HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Referer": "https://www.olx.ua/",
}

PRICE_RE = re.compile(r"([\d\s]+)(?:,\d+)?\s*(грн|UAH|€|EUR|\$|USD)", re.IGNORECASE)


def _parse_price(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    match = PRICE_RE.search(text)
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace("\xa0", "")
    currency_raw = match.group(2).upper()
    currency = "UAH" if currency_raw in ("ГРН", "UAH") else ("EUR" if currency_raw in ("€", "EUR") else "USD")
    try:
        return float(number), currency
    except ValueError:
        return None


async def fetch_listing_price(url: str) -> tuple[float, str] | None:
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=HEADERS) as session:
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
                return float(meta["content"]), (currency_meta["content"] if currency_meta else "UAH")
            except (ValueError, KeyError):
                pass

        # Debug fallback: log a snippet so we can see what actually came back
        title_tag = soup.find("title")
        logger.warning(
            "OLX listing: price not found. page_title=%r, snippet=%r",
            title_tag.get_text(strip=True) if title_tag else None,
            html[:500],
        )
        return None

    return _parse_price(text)


def _build_search_url(title_query: str, max_price: float | None, location: str, radius_km: int) -> str:
    query = title_query.strip().replace(" ", "-")
    base = f"https://www.olx.ua/uk/list/q-{query}/"
    params = []
    if max_price:
        params.append(f"search[filter_float_price:to]={int(max_price)}")
    if location:
        params.append(f"search[dist]={radius_km}")
    if params:
        base += "?" + "&".join(params)
    return base


async def search_listings(title_query: str, max_price: float | None, location: str, radius_km: int) -> list[dict]:
    url = _build_search_url(title_query, max_price, location, radius_km)
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=HEADERS) as session:
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
            href = "https://www.olx.ua" + href

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
            "currency": parsed_price[1] if parsed_price else "UAH",
            "location_text": location_text,
        })

    return results