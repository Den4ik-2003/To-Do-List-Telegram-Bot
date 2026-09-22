

import asyncio
import logging
import re
import statistics
from datetime import datetime, timedelta

import config.settings as cfg
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from config.settings import AI_DAILY_LIMIT
from database import resale as resale_db
from database import ai_usage as ai_usage_db
from services import ai_service, olx_service, resale_engine

logger = logging.getLogger("tasks_bot")


def _cfg(name: str, default):
    return getattr(cfg, name, default)


FETCH_CONCURRENCY = 3
REPORT_SIZE = 3                 # скільки найкращих варіантів у вечірньому звіті
CANDIDATE_TTL_HOURS = 36        # кандидати старші за це у звіт не потрапляють
PRICE_DROP_RATIO = 0.9          # повторний показ, якщо ціна впала на 10%+
SAME_PRICE_TOLERANCE = 0.05     # ±5% вважаємо тією самою ціною
DEFAULT_MIN_PROFIT = 200.0
DEFAULT_MIN_MARGIN = 15.0


def _max_ai_per_run() -> int:
    return int(_cfg("RESALE_MAX_AI_PER_MONITOR", 5))


def _min_score() -> float:
    return float(_cfg("RESALE_MIN_CANDIDATE_SCORE", 30))


# ---------------------------------------------------------
# Захист від одночасних запусків одного автопошуку
# ---------------------------------------------------------
_running: set[str] = set()


def try_acquire(monitor_id) -> bool:
    mid = str(monitor_id)
    if mid in _running:
        return False
    _running.add(mid)
    return True


def release(monitor_id) -> None:
    _running.discard(str(monitor_id))


# ---------------------------------------------------------
# Утиліти
# ---------------------------------------------------------
_JUNK_RE = re.compile(
    r"запчастин|на запчаст|не працю[єе]|розбит|нероб[оа]ч|на розбір|під відновлення|заблокован|icloud",
    re.IGNORECASE,
)
_BAD_COND_RE = re.compile(
    r"не працю|несправн|зламан|розбит|на запчастин|запчастин|під відновлення|заблокован",
    re.IGNORECASE,
)


def esc(text) -> str:
    """Екранування для legacy Markdown Telegram."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text if text is not None else ""))


def _money(value) -> str:
    if value is None:
        return "?"
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def split_words(raw) -> list[str]:
    return [w.strip().lower() for w in re.split(r"[,;\n]", raw or "") if w.strip()]


def _short(text, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_query(monitor: dict) -> str:
    base = monitor.get("keywords") or monitor.get("category") or monitor.get("name") or ""
    parts = [base, monitor.get("brand_model") or ""]  # brand_model — для старих автопошуків
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def _parse_hm(value, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except (ValueError, TypeError):
        return default


def schedule_times() -> tuple[tuple[int, int], tuple[int, int]]:
    midday = _parse_hm(_cfg("RESALE_MIDDAY_TIME", "12:00"), (12, 0))
    evening = _parse_hm(_cfg("RESALE_EVENING_TIME", "19:00"), (19, 0))
    return midday, evening


def next_run_text(now: datetime | None = None) -> str:
    now = now or datetime.now()
    (mh, mm), (eh, em) = schedule_times()
    slots = [("збір", mh, mm), ("звіт", eh, em)]
    best = None
    for offset in (0, 1):
        for label, h, m in slots:
            dt = (now + timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if dt > now and (best is None or dt < best[0]):
                best = (dt, label)
    dt, label = best
    day = "сьогодні" if dt.date() == now.date() else "завтра"
    return f"{day} о {dt:%H:%M} ({label})"


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "ще не запускався"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except ValueError:
        return "—"


# ---------------------------------------------------------
# Ринок і прибуток
# ---------------------------------------------------------

def market_stats(prices: list) -> tuple[float | None, int]:
    """Медіана цін з реальних оголошень OLX (з відкиданням викидів). (None, n) якщо даних мало."""
    p = sorted(x for x in prices if isinstance(x, (int, float)) and x > 0)
    n = len(p)
    if n < 5:
        return None, n
    if n >= 8:
        k = n // 5
        p = p[k: n - k]
    return float(statistics.median(p)), n


def estimate_sale(analysis: dict, market_median: float | None) -> float | None:
    lo, hi = _num(analysis.get("resale_price_min")), _num(analysis.get("resale_price_max"))
    ai_mid = (lo + hi) / 2 if lo is not None and hi is not None and hi > 0 else None
    if market_median and ai_mid:
        # AI не може завищувати ціну більш ніж на 25% над реальним ринком
        return min(0.6 * market_median + 0.4 * ai_mid, market_median * 1.25)
    if market_median:
        return market_median
    if ai_mid:
        return ai_mid * 0.9  # без підтвердження ринком — консервативно
    return None


def calc_profit(price: float, sale: float, us: dict) -> tuple[float, float, float]:
    costs = us["delivery_cost"] + us["packing_cost"] + sale * us["commission_percent"] / 100
    profit = sale - price - costs
    margin = profit / sale * 100 if sale else 0.0
    return round(profit), round(margin, 1), round(costs)


_DEMAND = {"висока": 1.0, "середня": 0.6, "низька": 0.2}
_SPEED = {"швидко": 1.0, "середньо": 0.6, "довго": 0.2}


def _condition_quality(text) -> float:
    t = (text or "").lower()
    if not t:
        return 0.6
    if "нов" in t or "відмін" in t or "ідеал" in t or "дуже добр" in t:
        return 1.0
    if "добр" in t:
        return 0.8
    if "задовіль" in t or "середн" in t:
        return 0.5
    return 0.6


def _adjust_learning(score: float, analysis: dict, monitor: dict) -> float:
    text = f"{(analysis.get('item_name') or '').lower()} {(analysis.get('item_brand') or '').lower()}"
    for kw in monitor.get("liked_keywords") or []:
        if kw and kw in text:
            score += 5
    for kw in monitor.get("disliked_keywords") or []:
        if kw and kw in text:
            score -= 10
    return score


def _is_blocked(analysis: dict, monitor: dict) -> bool:
    blocked = [b.lower() for b in (monitor.get("blocked_similar") or [])]
    if not blocked:
        return False
    text = f"{(analysis.get('item_name') or '').lower()} {(analysis.get('item_brand') or '').lower()}"
    return any(b in text for b in blocked)


def build_opportunity(
    listing: dict, analysis: dict, monitor: dict,
    market_median: float | None, market_n: int, us: dict,
) -> tuple[dict | None, str | None]:
    """Повертає (opportunity, None) або (None, причина_відсіву)."""
    price = _num(listing.get("price"))
    if price is None:
        return None, "no_price"
    if not analysis:
        return None, "no_analysis"

    text = f"{listing.get('title') or ''} {listing.get('description') or ''}".lower()
    if _JUNK_RE.search(text):
        return None, "junk"
    if _is_blocked(analysis, monitor):
        return None, "blocked"
    if analysis.get("authenticity_concern"):
        return None, "fake"
    risk = (analysis.get("risks") or {}).get("level")
    if risk == "високий":
        return None, "risk"
    if analysis.get("verdict") == "не варто":
        return None, "verdict"
    liquidity = analysis.get("liquidity")
    if liquidity == "низька":
        return None, "low_demand"

    conf = _num(analysis.get("confidence_percent"))
    conf = 60.0 if conf is None else conf
    if conf < 40:
        return None, "low_confidence"

    cond_text = " ".join([str(analysis.get("item_condition") or "")] + [str(d) for d in analysis.get("defects") or []])
    if _BAD_COND_RE.search(cond_text):
        return None, "bad_condition"

    if market_median and price < market_median * 0.4:
        return None, "suspiciously_cheap"

    est = estimate_sale(analysis, market_median)
    if est is None:
        return None, "no_estimate"
    market_verified = bool(market_median)
    if not market_verified and conf < 60:
        return None, "unverified"

    profit, margin, costs = calc_profit(price, est, us)
    min_profit = _num(monitor.get("min_profit"))
    min_profit = DEFAULT_MIN_PROFIT if min_profit is None else min_profit
    min_margin = _num(monitor.get("min_margin_percent"))
    min_margin = DEFAULT_MIN_MARGIN if min_margin is None else min_margin
    if profit < max(min_profit, 1):
        return None, "low_profit"
    if margin < min_margin:
        return None, "low_margin"
    min_resale = _num(monitor.get("min_resale_price"))
    if min_resale and est < min_resale:
        return None, "low_resale"

    # ---- рейтинг: прибуток > маржа > ціна відносно ринку > попит > стан > швидкість
    profit_norm = min(max(profit, 0) / max(min_profit * 3, 1500), 1.0)
    margin_norm = min(max(margin, 0) / 60.0, 1.0)
    if market_median:
        discount_norm = min(max((market_median - price) / market_median, 0) / 0.5, 1.0)
    else:
        discount_norm = 0.3
    demand = _DEMAND.get(liquidity, 0.5)
    speed = _SPEED.get(analysis.get("sale_speed"), 0.5)
    cond_q = _condition_quality(analysis.get("item_condition"))

    score = 100 * (
        0.35 * profit_norm + 0.25 * margin_norm + 0.15 * discount_norm
        + 0.10 * demand + 0.08 * cond_q + 0.07 * speed
    )
    score *= 0.7 + 0.3 * conf / 100
    if not market_verified:
        score *= 0.9
    extra = split_words(monitor.get("extra_keywords"))
    if extra and any(w in text for w in extra):
        score += 4
    score = max(0.0, min(100.0, _adjust_learning(score, analysis, monitor)))
    if score < _min_score():
        return None, "low_score"

    bits = []
    if market_median and price < market_median:
        bits.append(f"ціна на {round((1 - price / market_median) * 100)}% нижча за медіану ринку ({market_n} оголошень)")
    if liquidity == "висока":
        bits.append("високий попит")
    if analysis.get("sale_speed") == "швидко":
        bits.append("має швидко перепродатись")
    if not bits:
        bits.append((analysis.get("verdict") or "варіант з потенціалом").capitalize())
    why = "; ".join(bits)
    why = why[0].upper() + why[1:]

    return {
        "price": price,
        "est_sale": round(est),
        "profit": profit,
        "margin": margin,
        "costs": costs,
        "score": round(score, 1),
        "market_median": round(market_median) if market_median else None,
        "market_n": market_n,
        "market_verified": market_verified,
        "why": why,
    }, None


# ---------------------------------------------------------
# 1. ЗБІР (денний пошук)
# ---------------------------------------------------------

def _slim_listing(listing: dict) -> dict:
    keys = ("source", "url", "title", "price", "currency", "location_text", "views", "photos_count", "params")
    slim = {k: listing.get(k) for k in keys}
    slim["description"] = (listing.get("description") or "")[:800]
    slim["photos"] = (listing.get("photos") or [])[:3]
    return slim


async def scan_monitor(monitor: dict) -> dict:
    """Збирає і оцінює нові оголошення, зберігає кандидатів у БД. Нічого не надсилає."""
    res = {"error": None, "scanned": 0, "analyzed": 0, "selected": 0, "rejected": 0, "ai_limit_hit": False}
    uid = monitor["uid"]
    mid = str(monitor["_id"])
    try:
        await _scan(monitor, uid, mid, res)
    finally:
        await resale_db.set_run_info(mid, {k: res[k] for k in ("scanned", "analyzed", "selected", "error")})
    return res


async def _scan(monitor: dict, uid: int, mid: str, res: dict):
    query = build_query(monitor)
    if not query:
        res["error"] = "no_query"
        return
    if not ai_service.is_available():
        res["error"] = "ai_unavailable"
        return
    if await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT) <= 0:
        res["error"] = "ai_limit"
        return

    us = await resale_db.get_user_settings(uid)
    domain = monitor.get("domain", "olx.ua")
    location = monitor.get("location") or ""
    radius = monitor.get("radius_km") or (100 if location else 0)
    condition = monitor.get("condition")
    min_price = _num(monitor.get("min_price"))
    max_price = _num(monitor.get("max_price"))

    # Пошук БЕЗ верхньої межі ціни — потрібен для чесної медіани ринку.
    market_results = await olx_service.search_listings(query, None, location, radius, domain=domain, condition=condition)
    if market_results is None:
        res["error"] = "search_failed"
        return
    pool = {r["url"]: r for r in market_results if r.get("url")}
    if max_price:
        capped = await olx_service.search_listings(query, max_price, location, radius, domain=domain, condition=condition)
        for r in capped or []:
            if r.get("url"):
                pool.setdefault(r["url"], r)
    res["scanned"] = len(pool)
    if not pool:
        return

    market_prices = [r["price"] for r in market_results if r.get("price") and (r.get("currency") or "UAH") == "UAH"]
    median, market_n = market_stats(market_prices)

    exclude = split_words(monitor.get("exclude_words")) + [b.lower() for b in (monitor.get("blocked_similar") or [])]
    city = location.strip().lower()
    shown = resale_db.shown_map(monitor)
    known = await resale_db.get_known_candidates(mid)

    fresh: list[tuple[dict, float | None]] = []
    for url, r in pool.items():
        price = r.get("price")
        if price is None or (r.get("currency") or "UAH") != "UAH":
            continue
        if min_price and price < min_price:
            continue
        if max_price and price > max_price:
            continue
        title = (r.get("title") or "").lower()
        if any(w in title for w in exclude) or _JUNK_RE.search(title):
            continue
        loc_text = (r.get("location_text") or "").lower()
        if city and loc_text and city not in loc_text:
            continue

        drop_from = None
        if url in shown:
            old = shown[url]
            if old and price <= old * PRICE_DROP_RATIO:
                drop_from = old      # ціна суттєво впала — можна показати знову
            else:
                continue
        k = known.get(url)
        if k and not drop_from:
            st = k.get("status")
            if st in ("new", "shown", "dismissed", "gone"):
                continue
            if st == "rejected":
                old_price = k.get("price") or 0
                if old_price and abs(price - old_price) / old_price < SAME_PRICE_TOLERANCE:
                    continue
        fresh.append((r, drop_from))

    if not fresh:
        return

    # Спершу найдешевші відносно ринку — там найбільша ймовірність вигоди.
    fresh.sort(key=lambda x: (x[0]["price"] / median) if median else x[0]["price"])
    batch = fresh[: _max_ai_per_run()]
    drops = {r["url"]: d for r, d in batch}

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _fetch(r: dict):
        async with sem:
            try:
                return r, await olx_service.fetch_listing_details(r["url"])
            except Exception:
                logger.exception("resale: fetch_listing_details упав для %s", r["url"])
                return r, None

    fetched = await asyncio.gather(*[_fetch(r) for r, _ in batch])
    day = datetime.now().strftime("%Y-%m-%d")

    for r, details in fetched:
        url = r["url"]
        if not details or details.get("price") is None:
            continue
        price = details["price"]
        if (min_price and price < min_price) or (max_price and price > max_price):
            continue
        if await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT) <= 0:
            res["ai_limit_hit"] = True
            break

        listing = {
            "source": domain,
            "url": url,
            "title": details.get("title") or r.get("title"),
            "price": price,
            "currency": details.get("currency", r.get("currency", "UAH")),
            "description": details.get("description"),
            "location_text": details.get("location_text") or r.get("location_text"),
            "views": details.get("views"),
            "photos": details.get("photos") or [],
            "photos_count": details.get("photos_count"),
            "params": details.get("params") or [],
        }
        try:
            analysis = await resale_engine.analyze_listing(listing, monitor.get("min_margin_percent"))
        except Exception:
            logger.exception("resale: analyze_listing упав для %s", url)
            continue
        if not analysis:
            continue
        await ai_usage_db.increment_usage(uid)
        res["analyzed"] += 1

        opp, reason = build_opportunity(listing, analysis, monitor, median, market_n, us)
        if opp:
            now = datetime.now().isoformat()
            await resale_db.upsert_candidate(mid, uid, url, {
                **opp,
                "title": listing["title"],
                "currency": listing["currency"],
                "condition_text": _short(str(analysis.get("item_condition") or ""), 50),
                "listing": _slim_listing(listing),
                "analysis": analysis,
                "status": "new",
                "day": day,
                "found_at": now,
                "price_drop_from": drops.get(url),
                "recheck_fail": 0,
            })
            res["selected"] += 1
        else:
            await resale_db.upsert_candidate(mid, uid, url, {
                "status": "rejected", "reject_reason": reason,
                "price": price, "title": listing["title"], "day": day,
            })
            res["rejected"] += 1


# ---------------------------------------------------------
# 2. ПОВТОРНА ПЕРЕВІРКА кандидатів (вечір)
# ---------------------------------------------------------

async def recheck_candidates(monitor: dict, limit: int = 12):
    mid = str(monitor["_id"])
    cutoff = (datetime.now() - timedelta(hours=CANDIDATE_TTL_HOURS)).isoformat()
    cands = await resale_db.get_active_candidates(mid, cutoff, limit=limit)
    if not cands:
        return
    us = await resale_db.get_user_settings(monitor["uid"])
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _one(c: dict):
        async with sem:
            try:
                return c, await olx_service.fetch_listing_price(c["url"])
            except Exception:
                logger.exception("resale: fetch_listing_price упав для %s", c.get("url"))
                return c, None

    for c, fetched in await asyncio.gather(*[_one(c) for c in cands]):
        if fetched is None:
            fails = (c.get("recheck_fail") or 0) + 1
            if fails >= 2:
                await resale_db.update_candidate(c["_id"], {"status": "gone", "recheck_fail": fails})
            else:
                await resale_db.update_candidate(c["_id"], {"recheck_fail": fails})
            continue
        new_price = fetched[0]
        if abs(new_price - (c.get("price") or 0)) < 1:
            continue
        listing = dict(c.get("listing") or {})
        listing["price"] = new_price
        opp, reason = build_opportunity(
            listing, c.get("analysis") or {}, monitor, c.get("market_median"), c.get("market_n") or 0, us,
        )
        if opp:
            await resale_db.update_candidate(c["_id"], {
                **opp, "listing": listing, "recheck_fail": 0, "price_changed_from": c.get("price"),
            })
        else:
            await resale_db.update_candidate(c["_id"], {
                "status": "rejected", "reject_reason": f"price_changed:{reason}", "price": new_price,
            })


# ---------------------------------------------------------
# 3. ЗВІТ
# ---------------------------------------------------------

def opp_from_candidate(c: dict) -> dict:
    listing = dict(c.get("listing") or {})
    listing["price"] = c.get("price")
    listing["url"] = c.get("url")
    listing.setdefault("title", c.get("title"))
    return {
        "monitor_id": c.get("monitor_id"),
        "listing": listing,
        "analysis": c.get("analysis") or {},
        "score": c.get("score"),
        "margin": c.get("margin"),
        "profit": c.get("profit"),
    }


def format_report(monitor: dict, items: list[dict], day: dict, us: dict, error: str | None = None) -> str:
    name = esc(monitor.get("name") or monitor.get("category") or "автопошук")
    lines = [f"🔎 *Автопошук: {name}*", ""]

    if not items:
        lines.append("😐 Сьогодні якісних варіантів для перепродажу не знайшов.")
        lines.append("Слабкі й сумнівні оголошення я навмисно не додаю.")
    for i, c in enumerate(items, 1):
        lines.append(f"{i}. *{esc(_short(c.get('title'), 70))}* — {_money(c.get('price'))} грн")
        lines.append(f"   Очікуваний перепродаж: ~{_money(c.get('est_sale'))} грн")
        lines.append(f"   Потенційний прибуток: ~{_money(c.get('profit'))} грн")
        lines.append(f"   Маржа: ~{c.get('margin', 0):.0f}%")
        if c.get("condition_text"):
            lines.append(f"   Стан: {esc(c['condition_text'])}")
        if c.get("why"):
            lines.append(f"   Чому цікаво: {esc(c['why'])}")
        if c.get("price_drop_from"):
            lines.append(f"   📉 Ціна знизилась, було {_money(c['price_drop_from'])} грн")
        if not c.get("market_verified", True):
            lines.append("   ⚠️ Ринкова ціна не підтверджена іншими оголошеннями")
        lines.append("")

    if items and len(items) < REPORT_SIZE:
        lines.append(f"ℹ️ Якісних варіантів знайдено менше {REPORT_SIZE} ({len(items)}). Сумнівні не додавав.")
        lines.append("")
    if error == "search_failed":
        lines.append("⚠️ OLX був тимчасово недоступний під час пошуку.")
    elif error in ("ai_limit", "ai_unavailable"):
        lines.append("⚠️ AI-ліміт вичерпано або AI недоступний, нових оголошень не аналізував.")

    extra_cost = f"доставка ~{_money(us['delivery_cost'])} грн, пакування ~{_money(us['packing_cost'])} грн"
    if us["commission_percent"]:
        extra_cost += f", комісія {us['commission_percent']:.0f}%"
    lines.append(f"_Проаналізовано сьогодні: {day.get('found', 0)}. У прибутку враховано: {extra_cost}._")
    return "\n".join(lines).strip()


def ikb_report(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, c in enumerate(items, 1):
        cid = str(c["_id"])
        if c.get("url"):
            rows.append([InlineKeyboardButton(text=f"🔗 Відкрити оголошення {i}", url=c["url"])])
        rows.append([
            InlineKeyboardButton(text=f"🤖 Аналіз {i}", callback_data=f"rso_analyze:{cid}"),
            InlineKeyboardButton(text=f"⭐ Зберегти {i}", callback_data=f"rso_save:{cid}"),
            InlineKeyboardButton(text=f"❌ {i}", callback_data=f"rso_skip:{cid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_report(bot, monitor: dict, error: str | None = None) -> int:
    """Формує і надсилає окремий блок результатів автопошуку. Повертає кількість показаних."""
    mid = str(monitor["_id"])
    cutoff = (datetime.now() - timedelta(hours=CANDIDATE_TTL_HOURS)).isoformat()
    items = await resale_db.get_report_candidates(mid, cutoff, REPORT_SIZE)
    day = await resale_db.day_stats(mid, datetime.now().strftime("%Y-%m-%d"))
    us = await resale_db.get_user_settings(monitor["uid"])

    text = format_report(monitor, items, day, us, error)
    kb = ikb_report(items) if items else None
    try:
        await bot.send_message(monitor["uid"], text, reply_markup=kb)
    except Exception:
        logger.warning("resale: не вдалось надіслати з розміткою, пробую без неї", exc_info=True)
        plain = re.sub(r"[*_`\\]", "", text)
        await bot.send_message(monitor["uid"], plain, reply_markup=kb, parse_mode=None)

    if items:
        await resale_db.mark_shown(mid, items)
        await resale_db.increment_stat(mid, "found", len(items))
    return len(items)


# ---------------------------------------------------------
# Тексти для меню
# ---------------------------------------------------------

def format_scan_summary(res: dict, first: bool = False) -> str:
    err = res.get("error")
    if err == "ai_unavailable":
        return AI_ERROR_TEXT
    if err == "ai_limit":
        return "⚠️ Денний AI-ліміт вичерпано — пошук виконається автоматично, коли ліміт оновиться."
    if err == "search_failed":
        return "⚠️ OLX тимчасово недоступний, спробую пізніше автоматично."
    if err == "no_query":
        return "⚠️ У автопошуку не задано ключових слів чи категорії."
    (_, _), (eh, em) = schedule_times()
    text = (
        f"✅ {'Перший збір' if first else 'Збір'} завершено: на OLX переглянуто {res.get('scanned', 0)}, "
        f"проаналізовано AI {res.get('analyzed', 0)}, відібрано {res.get('selected', 0)}.\n"
        f"Звіт надійде ввечері (~{eh:02d}:{em:02d})."
    )
    if res.get("selected", 0) == 0:
        text += "\nПоки нічого вартого уваги, це нормально: я показую лише реально вигідні варіанти."
    return text


def format_monitor_card(m: dict, day: dict) -> str:
    active = m.get("status", "active") == "active"
    status = "🟢 активний" if active else "⏸ на паузі"
    lo, hi = m.get("min_price"), m.get("max_price")
    if lo and hi:
        price = f"купівля {_money(lo)}–{_money(hi)} грн"
    elif hi:
        price = f"купівля до {_money(hi)} грн"
    elif lo:
        price = f"купівля від {_money(lo)} грн"
    else:
        price = "без обмеження ціни"
    loc = m.get("location") or "вся Україна"

    crit = []
    if m.get("min_resale_price"):
        crit.append(f"перепродаж від {_money(m['min_resale_price'])} грн")
    if m.get("min_profit") is not None:
        crit.append(f"прибуток від {_money(m['min_profit'])} грн")
    if m.get("min_margin_percent") is not None:
        crit.append(f"маржа від {m['min_margin_percent']:.0f}%")

    last = m.get("last_run") or {}
    best = day.get("best_profit")
    lines = [
        f"🔥 *{esc(m.get('name') or m.get('category') or '—')}* — {status}",
        f"{price}, {esc(loc)}",
    ]
    if crit:
        lines.append(", ".join(crit))
    lines += [
        "",
        f"🔎 Знайдено оголошень (останній запуск): {last.get('scanned', 0)}",
        f"🎯 Відібрано (останній запуск): {last.get('selected', 0)}",
        f"📅 Можливостей за день: {day.get('selected', 0)} (проаналізовано {day.get('found', 0)})",
        f"💵 Найкращий прибуток сьогодні: {'~' + _money(best) + ' грн' if best is not None else '—'}",
        f"🕒 Останній запуск: {_fmt_dt(m.get('last_checked_at'))}",
        f"⏭ Наступний: {next_run_text() if active else '— (на паузі)'}",
    ]
    return "\n".join(lines)


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
        by_cat: dict = {}
        for m in monitors:
            cat = m.get("name") or m.get("category") or "—"
            by_cat[cat] = by_cat.get(cat, 0) + (m.get("stats") or {}).get("actual_profit", 0)
        if any(by_cat.values()):
            best_category = max(by_cat, key=by_cat.get)

    lines = [
        "📈 *Статистика перепродажу*", "",
        f"🔎 Показано можливостей у звітах: {found}",
        f"⭐ Збережено: {saved_n}",
        f"🛒 Куплено: {bought}",
        f"💰 Перепродано: {sold}",
        f"💵 Потенційний прибуток: ~{potential_profit:.0f} грн",
        f"✅ Фактичний прибуток: ~{actual_profit:.0f} грн",
    ]
    if avg_margin is not None:
        lines.append(f"📊 Середня маржа збережених: ~{avg_margin}%")
    if best_category:
        lines.append(f"🏆 Найвигідніший автопошук: {esc(best_category)}")
    return "\n".join(lines)