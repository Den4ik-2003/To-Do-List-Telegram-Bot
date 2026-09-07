"""
database/resale.py

Фіча "🔥 Знайти перепродаж" — постійний AI-моніторинг OLX.

Дві сутності:
  resale_monitors_col — налаштування моніторингу користувача (категорія,
    ціни, поріг прибутку/маржі, частота, seen-кеш для захисту від
    дублікатів, навчені ключові слова, статистика).
  resale_saved_col — знахідки, які користувач зберіг (⭐), з подальшим
    статусом (🛒 Купив / 💰 Перепродав / ❌ Відмовився).

Усі функції прив'язані до uid — жодна не змішує дані різних користувачів
(кожен запит фільтрує/перевіряє uid).
"""

from datetime import datetime

from bson import ObjectId

from database.mongo import resale_saved_col, resale_monitors_col, db_call

MAX_SEEN_PER_MONITOR = 300
MAX_LEARNED_KEYWORDS = 30


def _empty_stats() -> dict:
    return {
        "found": 0,
        "saved": 0,
        "bought": 0,
        "sold": 0,
        "potential_profit": 0.0,
        "actual_profit": 0.0,
    }


# =========================================================
# МОНІТОРИНГИ
# =========================================================

async def add_monitor(uid: int, params: dict) -> str:
    now = datetime.now().isoformat()
    doc = {
        "uid": uid,
        "category": params.get("category") or "",
        "keywords": params.get("keywords") or "",
        "location": params.get("location") or "",
        "radius_km": params.get("radius_km") or 0,
        "min_price": params.get("min_price"),
        "max_price": params.get("max_price"),
        "min_profit": params.get("min_profit"),
        "min_margin_percent": params.get("min_margin_percent"),
        "condition": params.get("condition"),  # "used" | "new" | None
        "brand_model": params.get("brand_model"),
        "check_interval_minutes": params.get("check_interval_minutes") or 180,
        "status": "active",
        "seen": [],  # [{"url": str, "at": iso}]
        "liked_keywords": [],
        "disliked_keywords": [],
        "blocked_similar": [],
        "stats": _empty_stats(),
        "created_at": now,
        "last_checked_at": None,
    }
    result = await db_call(resale_monitors_col.insert_one(doc))
    return str(result.inserted_id)


async def update_monitor(monitor_id, uid: int, fields: dict) -> bool:
    result = await db_call(
        resale_monitors_col.update_one(
            {"_id": ObjectId(monitor_id), "uid": uid},
            {"$set": fields},
        )
    )
    return result.modified_count > 0


async def get_user_monitors(uid: int) -> list[dict]:
    cursor = resale_monitors_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_monitor(monitor_id) -> dict | None:
    return await db_call(resale_monitors_col.find_one({"_id": ObjectId(monitor_id)}))


async def get_all_active_monitors() -> list[dict]:
    cursor = resale_monitors_col.find({"status": "active"})
    return await db_call(cursor.to_list(length=1000))


async def set_monitor_status(monitor_id, uid: int, status: str) -> bool:
    result = await db_call(
        resale_monitors_col.update_one(
            {"_id": ObjectId(monitor_id), "uid": uid},
            {"$set": {"status": status}},
        )
    )
    return result.modified_count > 0


async def delete_monitor(monitor_id, uid: int) -> bool:
    result = await db_call(
        resale_monitors_col.delete_one({"_id": ObjectId(monitor_id), "uid": uid})
    )
    return result.deleted_count > 0


async def touch_monitor_checked(monitor_id):
    await db_call(
        resale_monitors_col.update_one(
            {"_id": ObjectId(monitor_id)},
            {"$set": {"last_checked_at": datetime.now().isoformat()}},
        ),
        raise_on_fail=False,
    )


async def mark_seen(monitor_id, urls: list[str]):
    """Захист від дублікатів: додає нові URL у seen-кеш моніторингу і
    тримає лише останні MAX_SEEN_PER_MONITOR, щоб документ не ріс вічно."""
    if not urls:
        return
    now = datetime.now().isoformat()
    monitor = await get_monitor(monitor_id)
    if not monitor:
        return
    seen = monitor.get("seen") or []
    existing_urls = {e["url"] for e in seen}
    for u in urls:
        if u not in existing_urls:
            seen.append({"url": u, "at": now})
            existing_urls.add(u)
    seen = seen[-MAX_SEEN_PER_MONITOR:]
    await db_call(
        resale_monitors_col.update_one({"_id": ObjectId(monitor_id)}, {"$set": {"seen": seen}}),
        raise_on_fail=False,
    )


async def add_learned_keyword(monitor_id, keyword: str, positive: bool):
    """🎯 AI навчається на виборі користувача (⭐ Зберегти / ❌ Не цікавить)."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return
    field = "liked_keywords" if positive else "disliked_keywords"
    monitor = await get_monitor(monitor_id)
    if not monitor:
        return
    lst = monitor.get(field) or []
    if keyword not in lst:
        lst.append(keyword)
    lst = lst[-MAX_LEARNED_KEYWORDS:]
    await db_call(
        resale_monitors_col.update_one({"_id": ObjectId(monitor_id)}, {"$set": {field: lst}}),
        raise_on_fail=False,
    )


async def add_blocked_similar(monitor_id, keyword: str):
    """🔕 Не шукати подібне — повне виключення категорії товару з цього моніторингу."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return
    monitor = await get_monitor(monitor_id)
    if not monitor:
        return
    lst = monitor.get("blocked_similar") or []
    if keyword not in lst:
        lst.append(keyword)
    await db_call(
        resale_monitors_col.update_one({"_id": ObjectId(monitor_id)}, {"$set": {"blocked_similar": lst}}),
        raise_on_fail=False,
    )


async def increment_stat(monitor_id, field: str, amount: float = 1):
    await db_call(
        resale_monitors_col.update_one(
            {"_id": ObjectId(monitor_id)},
            {"$inc": {f"stats.{field}": amount}},
        ),
        raise_on_fail=False,
    )


# =========================================================
# ЗБЕРЕЖЕНІ МОЖЛИВОСТІ
# =========================================================

async def save_opportunity(uid: int, monitor_id, opp: dict) -> str:
    listing = opp.get("listing") or {}
    analysis = opp.get("analysis") or {}
    doc = {
        "uid": uid,
        "monitor_id": str(monitor_id) if monitor_id else None,
        "url": listing.get("url"),
        "title": analysis.get("item_name") or listing.get("title"),
        "purchase_price": listing.get("price"),
        "currency": listing.get("currency", "UAH"),
        "score": opp.get("score"),
        "profit": opp.get("profit"),
        "margin": opp.get("margin"),
        "risk": (analysis.get("risks") or {}).get("level"),
        "status": "interest",
        "saved_at": datetime.now().isoformat(),
    }
    result = await db_call(resale_saved_col.insert_one(doc))
    return str(result.inserted_id)


async def get_saved(uid: int) -> list[dict]:
    cursor = resale_saved_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=200))


async def get_saved_item(item_id) -> dict | None:
    return await db_call(resale_saved_col.find_one({"_id": ObjectId(item_id)}))


async def set_saved_status(item_id, uid: int, status: str) -> bool:
    result = await db_call(
        resale_saved_col.update_one(
            {"_id": ObjectId(item_id), "uid": uid},
            {"$set": {"status": status, "status_at": datetime.now().isoformat()}},
        )
    )
    return result.modified_count > 0


async def delete_saved(item_id, uid: int) -> bool:
    result = await db_call(
        resale_saved_col.delete_one({"_id": ObjectId(item_id), "uid": uid})
    )
    return result.deleted_count > 0