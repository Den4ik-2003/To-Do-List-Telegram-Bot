"""
Сервіс пошуку можливостей для перепродажу на OLX.

Відповідає за одну дію: за критеріями користувача (бюджет, категорія,
мін. прибуток, мін. маржа, к-сть результатів) знайти оголошення на OLX
і прогнати їх через AI resale-аналіз (services/resale_engine.py),
повернувши handler'у (handlers/resale.py) готовий список словників
у форматі, який очікує _fmt_item():

{
    "title": str,
    "purchase_price": float,
    "currency": str,
    "market_price": float,
    "resale_price": float,
    "profit": float,
    "margin": float,          # у %
    "url": str,
    "perspective": int | None,  # 0-10, з AI-аналізу resale_score/10
    "risk": str,               # "низький"/"середній"/"високий"
    "reasoning": str,
}
"""

import asyncio
import logging

from services import olx_service
from services import resale_engine

logger = logging.getLogger("tasks_bot")

# Скільки оголошень максимум прогнати через AI за один пошук
# (щоб не влетіти в ліміти/час очікування користувача)
MAX_CANDIDATES = 15

# Скільки AI-аналізів виконувати паралельно одночасно
_ANALYSIS_CONCURRENCY = 3

_SPEED_ORDER = {"швидко": 0, "середньо": 1, "довго": 2}
_RISK_ORDER = {"низький": 0, "середній": 1, "високий": 2}


async def _analyze_one(listing: dict, min_margin_percent: float | None, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        try:
            analysis = await resale_engine.analyze_listing(listing, min_margin_percent)
        except Exception:
            logger.exception("resale_service: analyze_listing failed for %s", listing.get("url"))
            return None

    if not analysis:
        return None

    price = listing.get("price") or 0
    resale_min = analysis.get("resale_price_min")
    resale_max = analysis.get("resale_price_max")
    if resale_min is None or resale_max is None:
        return None

    market_price = (resale_min + resale_max) / 2
    profit = analysis.get("expected_profit")
    if profit is None:
        profit = market_price - price

    margin = (profit / market_price * 100) if market_price else 0
    score = analysis.get("resale_score")
    perspective = round(score / 10) if isinstance(score, (int, float)) else None
    risk_level = (analysis.get("risks") or {}).get("level", "середній")

    reasoning_parts = []
    if analysis.get("verdict"):
        reasoning_parts.append(analysis["verdict"].capitalize())
    args = (analysis.get("negotiation") or {}).get("arguments") or []
    if args:
        reasoning_parts.append(args[0])
    reasoning = ". ".join(reasoning_parts)

    return {
        "title": analysis.get("item_name") or listing.get("title") or "—",
        "purchase_price": price,
        "currency": listing.get("currency", "UAH"),
        "market_price": market_price,
        "resale_price": market_price,
        "profit": profit,
        "margin": margin,
        "url": listing.get("url") or listing.get("id") or "",
        "perspective": perspective,
        "risk": risk_level,
        "reasoning": reasoning,
        "_speed": analysis.get("sale_speed", "середньо"),
    }


async def find_opportunities(
    budget: float | None,
    category: str | None,
    min_profit: float | None,
    min_margin: float | None,
    count: int,
) -> list[dict]:
    """
    Головна точка входу для кнопки "🔎 Знайти перепродаж".

    1. Шукає кандидатів на OLX за бюджетом/категорією.
    2. Прогонає до MAX_CANDIDATES з них через AI resale-аналіз паралельно.
    3. Фільтрує за min_profit / min_margin.
    4. Сортує за маржею (дефолтне сортування) і повертає top `count`.
    """
    # TODO: перевір сигнатуру — підлаштуй під реальний olx_service.py
    try:
        listings = await olx_service.search_listings(
            query=category,
            max_price=budget,
            limit=MAX_CANDIDATES,
        )
    except Exception:
        logger.exception("resale_service: OLX search failed (budget=%s, category=%s)", budget, category)
        return []

    if not listings:
        return []

    sem = asyncio.Semaphore(_ANALYSIS_CONCURRENCY)
    tasks = [_analyze_one(listing, min_margin, sem) for listing in listings[:MAX_CANDIDATES]]
    analyzed = await asyncio.gather(*tasks)

    results = [r for r in analyzed if r is not None]

    if min_profit is not None:
        results = [r for r in results if r["profit"] >= min_profit]
    if min_margin is not None:
        results = [r for r in results if r["margin"] >= min_margin]

    results.sort(key=lambda r: r["margin"], reverse=True)

    for r in results:
        r.pop("_speed", None) if False else None  # лишаємо _speed для сортування нижче

    return results[:count] if count else results


def sort_opportunities(results: list[dict], sort_key: str) -> list[dict]:
    """Пересортовує вже знайдені результати за вибором користувача (rs_sort:*)."""
    if sort_key == "profit":
        return sorted(results, key=lambda r: r.get("profit", 0), reverse=True)
    if sort_key == "margin":
        return sorted(results, key=lambda r: r.get("margin", 0), reverse=True)
    if sort_key == "perspective":
        return sorted(results, key=lambda r: (r.get("perspective") or 0), reverse=True)
    if sort_key == "risk":
        return sorted(results, key=lambda r: _RISK_ORDER.get(r.get("risk"), 1))
    if sort_key == "speed":
        return sorted(results, key=lambda r: _SPEED_ORDER.get(r.get("_speed"), 1))
    return results