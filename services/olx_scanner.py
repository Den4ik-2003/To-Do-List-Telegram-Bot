"""
services/olx_scanner.py

Спільна логіка для:
  🔨 Аукціон навпаки  — одноразовий пошук найдешевших варіантів (без AI)
  🧲 Злови помилку     — одноразовий AI-скан на предмет недооцінених оголошень
  🧠 AI Scanner        — те саме, але як фонова періодична підписка
  🔎 Схожі (AI)        — той самий скан, запущений за назвою вже відстежуваного товару

Свідомо НЕ дублює AI-логіку: оцінка вигідності робиться тим самим
resale_engine.analyze_listing() / resale_engine.rank_top_deals(), що вже
використовується в AI Resale Hunter (handlers/olx.py, _run_analysis /
olx_top_deals_cb). Кожна AI-перевірка тут списує денний ліміт користувача
(ai_usage_db) так само, як і ручний аналіз одного оголошення — це свідомо:
без цього AI Scanner міг би непомітно "з'їсти" весь денний ліміт користувача
за одну фонову перевірку.
"""

import asyncio
import logging

from database import olx as olx_db
from database import ai_usage as ai_usage_db
from config.settings import AI_DAILY_LIMIT
from services import olx_service
from services import ai_service
from services import resale_engine

logger = logging.getLogger("tasks_bot")

# Скільки нових оголошень (найдешевших спершу) прожовувати через AI за один
# запуск — обмежено, щоб один ручний запит або один цикл AI Scanner-а не
# спалював увесь денний AI-ліміт користувача одразу.
MAX_CANDIDATES_TO_SCAN = 6

# Скільки повних сторінок оголошень тягнемо ОДНОЧАСНО. Кожен фетч важить
# ~1.4 МБ HTML і триває 40-90с через анти-бот захист OLX (curl_cffi
# impersonation), тому послідовний фетч 6-8 кандидатів розтягувався на
# 5-7+ хвилин. Паралелимо з невеликим лімітом, щоб не тригернути rate-limit
# чи бан з боку OLX.
FETCH_CONCURRENCY = 4


async def cheapest_matches(
    query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
    domain: str = "olx.ua",
    condition: str | None = None,
    limit: int = 10,
) -> list[dict] | None:
    """🔨 Аукціон навпаки: чистий пошук + сортування за зростанням ціни, без AI. None -> технічний збій."""
    results = await olx_service.search_listings(query, max_price, location, radius_km, domain=domain, condition=condition)
    if results is None:
        return None
    return olx_service.sort_by_price(results)[:limit]


async def _fetch_details_safe(sem: asyncio.Semaphore, candidate: dict) -> tuple[dict, dict | None]:
    """Обгортка над fetch_listing_details з обмеженням паралельності. Ніколи не
    кидає виняток назовні — технічний збій одного кандидата не має валити
    весь скан, просто пропускаємо його далі."""
    async with sem:
        try:
            details = await olx_service.fetch_listing_details(candidate["url"])
        except Exception:
            logger.exception("scan_for_deals: fetch_listing_details упав для %s", candidate["url"])
            details = None
        return candidate, details


async def scan_for_deals(
    uid: int,
    query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
    domain: str = "olx.ua",
    progress_cb=None,
):
    """
    Ядро 🧲 Злови помилку, 🧠 AI Scanner та 🔎 Схожі (AI): бере найдешевші
    свіжі оголошення за запитом (найбільша ймовірність помилки продавця —
    саме серед них), тягне повні деталі ПАРАЛЕЛЬНО (див. FETCH_CONCURRENCY) й
    прожовує через ТОЙ САМИЙ AI resale-аналіз, що й ручна оцінка одного
    оголошення, потім ранжує тим самим rank_top_deals, що вже
    використовується для 🏆 TOP Deals.

    progress_cb: опціональний async callable(done: int, total: int),
    викликається після кожного проаналізованого (або пропущеного) кандидата —
    щоб виклик міг показати користувачу живий прогрес замість "тиші" на
    кілька хвилин. Збій самого callback'а не перериває скан.

    Повертає (ranked, error). ranked — список словників {"listing":.., "analysis":..},
    відсортований від найцікавішого. error ("ai_unavailable"/"ai_limit"/"search_failed")
    не None, якщо перевірку взагалі не вдалося виконати — щоб виклик показав
    чесне повідомлення, а не тихо "нічого не знайдено".
    """
    if not ai_service.is_available():
        return None, "ai_unavailable"

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return None, "ai_limit"

    results = await olx_service.search_listings(query, max_price, location, radius_km, domain=domain)
    if results is None:
        return None, "search_failed"
    if not results:
        return [], None

    candidates = olx_service.sort_by_price(results)[:MAX_CANDIDATES_TO_SCAN]
    settings = await olx_db.get_user_settings(uid)

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    fetched = await asyncio.gather(*[_fetch_details_safe(sem, c) for c in candidates])

    pseudo_trackers = []
    total = len(fetched)
    for done, (c, details) in enumerate(fetched, start=1):
        if not details or details.get("price") is None:
            if progress_cb:
                await _safe_progress(progress_cb, done, total)
            continue

        remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
        if remaining <= 0:
            break

        listing = {
            "source": domain,
            "url": c["url"],
            "title": details.get("title") or c.get("title"),
            "price": details["price"],
            "currency": details.get("currency", c.get("currency", "UAH")),
            "description": details.get("description"),
            "location_text": details.get("location_text") or c.get("location_text"),
            "views": details.get("views"),
            "photos": details.get("photos") or [],
            "photos_count": details.get("photos_count"),
            "params": details.get("params") or [],
        }

        try:
            analysis = await resale_engine.analyze_listing(listing, settings.get("min_margin_percent"))
        except Exception:
            logger.exception("scan_for_deals: resale_engine.analyze_listing упав для %s", c["url"])
            if progress_cb:
                await _safe_progress(progress_cb, done, total)
            continue
        if not analysis:
            if progress_cb:
                await _safe_progress(progress_cb, done, total)
            continue

        await ai_usage_db.increment_usage(uid)
        pseudo_trackers.append({
            "_id": None,
            "url": listing["url"],
            "title": listing["title"],
            "last_price": listing["price"],
            "currency": listing["currency"],
            "resale_analysis": analysis,
            "favorited": False,
            "status": "watching",
            "_listing": listing,  # додатковий ключ для форматування; rank_top_deals читає лише відомі йому поля
        })

        if progress_cb:
            await _safe_progress(progress_cb, done, total)

    if not pseudo_trackers:
        return [], None

    try:
        ranked = resale_engine.rank_top_deals(pseudo_trackers)
    except Exception:
        logger.exception("scan_for_deals: resale_engine.rank_top_deals упав")
        return [], None

    return ranked, None


async def _safe_progress(progress_cb, done: int, total: int) -> None:
    try:
        await progress_cb(done, total)
    except Exception:
        logger.exception("scan_for_deals: progress_cb упав")