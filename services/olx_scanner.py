# services/olx_scanner.py
import asyncio
import logging
from database import olx as olx_db
from database import ai_usage as ai_usage_db
from config.settings import AI_DAILY_LIMIT
from services import olx_service
from services import ai_service
from services import resale_engine
logger = logging.getLogger('tasks_bot')
MAX_CANDIDATES_TO_SCAN = 6
FETCH_CONCURRENCY = 4

async def cheapest_matches(query: str, max_price: float | None, location: str, radius_km: int, domain: str='olx.ua', condition: str | None=None, limit: int=10) -> list[dict] | None:
    results = await olx_service.search_listings(query, max_price, location, radius_km, domain=domain, condition=condition)
    if results is None:
        return None
    return olx_service.sort_by_price(results)[:limit]

async def _fetch_details_safe(sem: asyncio.Semaphore, candidate: dict) -> tuple[dict, dict | None]:
    async with sem:
        try:
            details = await olx_service.fetch_listing_details(candidate['url'])
        except Exception:
            logger.exception('scan_for_deals: fetch_listing_details упав для %s', candidate['url'])
            details = None
        return (candidate, details)

async def scan_for_deals(uid: int, query: str, max_price: float | None, location: str, radius_km: int, domain: str='olx.ua', progress_cb=None, limit: int=3):
    if not ai_service.is_available():
        return (None, 'ai_unavailable')
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return (None, 'ai_limit')
    results = await olx_service.search_listings(query, max_price, location, radius_km, domain=domain)
    if results is None:
        return (None, 'search_failed')
    if not results:
        return ([], None)
    candidates = olx_service.sort_by_price(results)[:MAX_CANDIDATES_TO_SCAN]
    settings = await olx_db.get_user_settings(uid)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    fetched = await asyncio.gather(*[_fetch_details_safe(sem, c) for c in candidates])
    pseudo_trackers = []
    total = len(fetched)
    for done, (c, details) in enumerate(fetched, start=1):
        if not details or details.get('price') is None:
            if progress_cb:
                await _safe_progress(progress_cb, done, total, len(pseudo_trackers))
            continue
        remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
        if remaining <= 0:
            break
        listing = {'source': domain, 'url': c['url'], 'title': details.get('title') or c.get('title'), 'price': details['price'], 'currency': details.get('currency', c.get('currency', 'UAH')), 'description': details.get('description'), 'location_text': details.get('location_text') or c.get('location_text'), 'views': details.get('views'), 'photos': details.get('photos') or [], 'photos_count': details.get('photos_count'), 'params': details.get('params') or []}
        try:
            analysis = await resale_engine.analyze_listing(listing, settings.get('min_margin_percent'))
        except Exception:
            logger.exception('scan_for_deals: resale_engine.analyze_listing упав для %s', c['url'])
            if progress_cb:
                await _safe_progress(progress_cb, done, total, len(pseudo_trackers))
            continue
        if not analysis:
            if progress_cb:
                await _safe_progress(progress_cb, done, total, len(pseudo_trackers))
            continue
        await ai_usage_db.increment_usage(uid)
        pseudo_trackers.append({'_id': None, 'url': listing['url'], 'title': listing['title'], 'last_price': listing['price'], 'currency': listing['currency'], 'resale_analysis': analysis, 'favorited': False, 'status': 'watching', '_listing': listing})
        if progress_cb:
            await _safe_progress(progress_cb, done, total, len(pseudo_trackers))
    if not pseudo_trackers:
        return ([], None)
    try:
        ranked = resale_engine.rank_top_deals(pseudo_trackers, limit=limit)
    except Exception:
        logger.exception('scan_for_deals: resale_engine.rank_top_deals упав')
        return ([], None)
    return (ranked, None)

async def _safe_progress(progress_cb, done: int, total: int, found: int) -> None:
    try:
        await progress_cb(done, total, found)
    except Exception:
        logger.exception('scan_for_deals: progress_cb упав')