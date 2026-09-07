"""
services/resale_service.py

Ядро "🔥 Знайти перепродаж". НЕ дублює AI resale-аналіз — увесь пошук на
OLX і резонансна оцінка вигідності беруться з уже робочого
services/olx_scanner.scan_for_deals(), яке саме по собі викликає
services/resale_engine.analyze_listing() (той самий AI resale hunter, що
й у "📉 OLX Ціни"). Тут лишається тільки специфічна для моніторингу
логіка: фільтри за критеріями користувача, захист від сміття, навчання
AI на виборі користувача, форматування сповіщення й pending-кеш для
кнопок під сповіщенням.
"""

import logging
import re
import secrets
from collections import OrderedDict

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from services import olx_scanner

logger = logging.getLogger("tasks_bot")

# Скільки найдешевших кандидатів прогонати через AI за один цикл моніторингу.
MAX_CANDIDATES_PER_SCAN = 10

_RISK_EMOJI = {"низький": "🟢", "середній": "🟡", "високий": "🔴"}
_DEMAND_EMOJI = {"висока": "🔥", "середня": "🟡", "низька": "🔻"}

# Явні ознаки сміття/непридатного товару (п.13 ТЗ). Свідомо консервативний
# список — краще пропустити спірний варіант, ніж відкинути хороший.
_JUNK_TITLE_RE = re.compile(
    r"запчастин|на запчаст|не працю[єе]|розбит[аоий]|нероб[оа]ч",
    re.IGNORECASE,
)

# ---------------------------------------------------------
# Pending-кеш: короткоживучі дані знахідки для кнопок під сповіщенням
# (Telegram callback_data обмежений 64 байтами — не влізе повний listing).
# ---------------------------------------------------------
_MAX_PENDING = 2000
_pending: "OrderedDict[str, dict]" = OrderedDict()


def register_pending(monitor_id, opp: dict) -> str:
    pid = secrets.token_hex(6)
    _pending[pid] = {"monitor_id": str(monitor_id), **opp}
    if len(_pending) > _MAX_PENDING:
        _pending.popitem(last=False)
    return pid


def get_pending(pid: str) -> dict | None:
    return _pending.get(pid)


def _is_junk(listing: dict, analysis: dict) -> bool:
    title = (listing.get("title") or "").lower()
    if _JUNK_TITLE_RE.search(title):
        return True
    if not listing.get("description") and not listing.get("photos"):
        return True
    if analysis.get("resale_score") is None:
        return True
    return False


def _is_blocked(analysis: dict, monitor: dict) -> bool:
    blocked = [b.lower() for b in (monitor.get("blocked_similar") or [])]
    if not blocked:
        return False
    text = f"{(analysis.get('item_name') or '').lower()} {(analysis.get('item_brand') or '').lower()}"
    return any(b in text for b in blocked)


def _margin_percent(analysis: dict, listing: dict) -> float:
    resale_min = analysis.get("resale_price_min")
    resale_max = analysis.get("resale_price_max")
    price = listing.get("price") or 0
    if resale_min is None or resale_max is None:
        return 0.0
    market = (resale_min + resale_max) / 2
    profit = analysis.get("expected_profit")
    if profit is None:
        profit = market - price
    return round((profit / market * 100) if market else 0, 1)


def _adjust_score(base_score: float, analysis: dict, monitor: dict) -> float:
    """🎯 AI навчається на виборі користувача — м'яке коригування рейтингу
    на основі раніше збережених/відхилених знахідок ЦЬОГО моніторингу."""
    text = f"{(analysis.get('item_name') or '').lower()} {(analysis.get('item_brand') or '').lower()}"
    score = base_score
    for kw in monitor.get("liked_keywords") or []:
        if kw and kw in text:
            score += 5
    for kw in monitor.get("disliked_keywords") or []:
        if kw and kw in text:
            score -= 10
    return max(0, min(100, score))


def _build_query(monitor: dict) -> str:
    parts = [monitor.get("category") or "", monitor.get("keywords") or "", monitor.get("brand_model") or ""]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


async def scan_monitor(monitor: dict) -> tuple[list[dict], str | None]:
    """
    Повертає (opportunities, error). opportunities — список словників
    {"listing":.., "analysis":.., "score":.., "margin":.., "profit":..},
    відсортований від найкращого. error ("ai_unavailable"/"ai_limit"/
    "search_failed") — якщо перевірку взагалі не вдалось виконати.
    """
    query = _build_query(monitor)
    if not query:
        return [], "no_query"

    ranked, error = await olx_scanner.scan_for_deals(
        monitor["uid"], query, monitor.get("max_price"),
        monitor.get("location", ""), monitor.get("radius_km", 0),
        domain=monitor.get("domain", "olx.ua"),
        limit=MAX_CANDIDATES_PER_SCAN,
    )
    if error:
        return [], error
    if not ranked:
        return [], None

    seen_urls = {e["url"] for e in (monitor.get("seen") or [])}

    opportunities = []
    for item in ranked:
        listing = (item.get("tracker") or {}).get("_listing") or {}
        analysis = item.get("analysis") or {}
        url = listing.get("url")
        if not url or url in seen_urls:
            continue
        if _is_junk(listing, analysis):
            continue
        if _is_blocked(analysis, monitor):
            continue

        price = listing.get("price")
        if monitor.get("min_price") is not None and price is not None and price < monitor["min_price"]:
            continue

        margin = _margin_percent(analysis, listing)
        profit = analysis.get("expected_profit")
        if profit is None:
            resale_min, resale_max = analysis.get("resale_price_min"), analysis.get("resale_price_max")
            profit = ((resale_min + resale_max) / 2 - (price or 0)) if resale_min is not None and resale_max is not None else None

        if monitor.get("min_profit") is not None and (profit is None or profit < monitor["min_profit"]):
            continue
        if monitor.get("min_margin_percent") is not None and margin < monitor["min_margin_percent"]:
            continue

        score = _adjust_score(analysis.get("resale_score", 0), analysis, monitor)
        opportunities.append({
            "listing": listing, "analysis": analysis,
            "score": score, "margin": margin, "profit": profit,
        })

    opportunities.sort(key=lambda o: o["score"], reverse=True)
    return opportunities, None


# =========================================================
# Форматування сповіщення (п.9 ТЗ)
# =========================================================

def format_notification(opp: dict) -> str:
    listing, analysis = opp["listing"], opp["analysis"]
    currency = listing.get("currency", "UAH")
    price = listing.get("price")
    resale_min, resale_max = analysis.get("resale_price_min"), analysis.get("resale_price_max")
    market = (resale_min + resale_max) / 2 if resale_min is not None and resale_max is not None else None
    profit, margin, score = opp.get("profit"), opp.get("margin"), opp.get("score")
    risk = (analysis.get("risks") or {}).get("level", "невідомо")
    demand = analysis.get("liquidity")
    speed = analysis.get("sale_speed")
    verdict = analysis.get("verdict") or ""
    args = (analysis.get("negotiation") or {}).get("arguments") or []
    missing = analysis.get("missing_data") or []

    lines = [
        "🔥 *ЗНАЙДЕНО МОЖЛИВІСТЬ ДЛЯ ПЕРЕПРОДАЖУ*", "",
        f"📦 {analysis.get('item_name') or listing.get('title') or '—'}",
        f"💰 Купівля: {price:.0f} {currency}" if price is not None else "💰 Купівля: невідомо",
    ]
    if market is not None:
        lines.append(f"📊 Ринок: ~{market:.0f} {currency}")
    if resale_max is not None:
        lines.append(f"🔄 Перепродаж: ~{resale_max:.0f} {currency}")
    if profit is not None:
        lines.append(f"💵 Потенційний прибуток: ~{profit:.0f} {currency}")
    if margin is not None:
        lines.append(f"📈 Маржа: ~{margin:.0f}%")

    lines.append("")
    lines.append(f"🔥 *Оцінка можливості: {score}/100*")
    lines.append(f"{_RISK_EMOJI.get(risk, '⚪️')} Ризик: {risk}")
    if demand:
        lines.append(f"{_DEMAND_EMOJI.get(demand, '')} Попит: {demand}".strip())
    if speed:
        lines.append(f"⚡ Швидкість продажу: {speed}")

    why = verdict.capitalize()
    if args:
        why = (why + ". " if why else "") + args[0]
    if why:
        lines.append("")
        lines.append("*Чому цікаво:*")
        lines.append(why)

    check = "; ".join(missing[:3]) if missing else "серійний номер/IMEI, реальний стан, комплектацію особисто перед покупкою"
    lines.append("")
    lines.append("*Перевірити:*")
    lines.append(check)

    if listing.get("url"):
        lines.append("")
        lines.append(f"🔗 {listing['url']}")

    lines.append(
        "\n_Витрати на доставку/ремонт враховані лише якщо очевидні з оголошення; "
        "інше AI не вигадує і позначає як невраховане._"
    )
    return "\n".join(lines).strip()


def ikb_notification(pending_id: str, url: str | None) -> InlineKeyboardMarkup:
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="🔗 Відкрити", url=url)])
    rows.append([InlineKeyboardButton(text="🤖 Детальний аналіз", callback_data=f"rso_analyze:{pending_id}")])
    rows.append([
        InlineKeyboardButton(text="⭐ Зберегти", callback_data=f"rso_save:{pending_id}"),
        InlineKeyboardButton(text="❌ Не цікавить", callback_data=f"rso_skip:{pending_id}"),
    ])
    rows.append([InlineKeyboardButton(text="🔕 Не шукати подібне", callback_data=f"rso_block:{pending_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================================================
# Статистика (п.11 ТЗ)
# =========================================================

def build_statistics_text(monitors: list[dict], saved: list[dict]) -> str:
    found = sum((m.get("stats") or {}).get("found", 0) for m in monitors)
    saved_n = len(saved)
    bought = sum(1 for s in saved if s.get("status") == "bought")
    sold = sum(1 for s in saved if s.get("status") == "sold")
    potential_profit = sum(s.get("profit") or 0 for s in saved if s.get("status") in ("interest", "bought"))
    actual_profit = sum(s.get("profit") or 0 for s in saved if s.get("status") == "sold")
    margins = [s.get("margin") for s in saved if s.get("margin") is not None]
    avg_margin = round(sum(margins) / len(margins), 1) if margins else None

    best_category = None
    if monitors:
        by_cat = {}
        for m in monitors:
            cat = m.get("category") or "—"
            profit = (m.get("stats") or {}).get("actual_profit", 0)
            by_cat[cat] = by_cat.get(cat, 0) + profit
        if any(by_cat.values()):
            best_category = max(by_cat, key=by_cat.get)

    lines = [
        "📈 *Статистика перепродажу*", "",
        f"🔎 Знайдено можливостей: {found}",
        f"⭐ Збережено: {saved_n}",
        f"🛒 Куплено: {bought}",
        f"💰 Перепродано: {sold}",
        f"💵 Потенційний прибуток: ~{potential_profit:.0f} грн",
        f"✅ Фактичний прибуток: ~{actual_profit:.0f} грн",
    ]
    if avg_margin is not None:
        lines.append(f"📊 Середня маржа збережених: ~{avg_margin}%")
    if best_category:
        lines.append(f"🏆 Найвигідніша категорія: {best_category}")
    lines.append(
        "\n_Примітка: скільки разів відкрито «🔗 Відкрити» Telegram не повідомляє "
        "боту (це звичайна URL-кнопка), тому ця метрика не відстежується._"
    )
    return "\n".join(lines)