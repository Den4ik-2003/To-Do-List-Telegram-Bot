import hashlib
from bson import ObjectId
from datetime import datetime, timedelta

from database.mongo import (
    olx_tracked_col,
    olx_deals_col,
    olx_user_settings_col,
    olx_search_stats_col,
    db_call,
)


def _content_hash(title: str | None, price: float | None, description: str | None, photos: list | None) -> str:
    """
    Хеш контенту оголошення (назва+ціна+опис+список фото).
    Кешований AI-висновок прив'язаний не лише до tracker_id і TTL, а й до
    цього хешу. Якщо оголошення змінилося — кеш інвалідується миттєво.
    """
    raw = f"{title or ''}|{price or ''}|{description or ''}|{','.join(photos or [])}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def add_listing_tracker(
    uid: int,
    url: str,
    last_price: float | None,
    currency: str,
    title: str | None = None,
    description: str | None = None,
    location_text: str | None = None,
    views: int | None = None,
    photos_count: int | None = None,
    photos: list | None = None,
    params: list | None = None,
    own_listing: bool = False,
) -> str:
    now = datetime.now().isoformat()
    doc = {
        "uid": uid,
        "type": "listing",
        "url": url,
        "last_price": last_price,
        "currency": currency,
        "title": title,
        "description": description,
        "location_text": location_text,
        "views": views,
        "photos_count": photos_count,
        "photos": photos or [],
        "params": params or [],
        "content_hash": _content_hash(title, last_price, description, photos),
        "resale_analysis": None,
        "resale_analysis_at": None,
        "negotiation_messages": None,  # {"soft": str, "optimal": str, "aggressive": str}
        "status": "watching",  # watching | bought | sold
        "favorited": False,
        # НОВЕ: історія ціни. OLX технічно не віддає ціну "заднім числом" —
        # історія до моменту додавання в моніторинг НЕ вигадується, тут
        # завжди лежить один-єдиний початковий запис (ціна на момент
        # додавання), а далі кожна реальна зміна ціни, зафіксована
        # scheduler-джобою, дописується сюди.
        "price_history": [{"price": last_price, "currency": currency, "at": now}] if last_price is not None else [],
        # НОВЕ: чи це власне оголошення користувача (розділ 📋 Мої оголошення),
        # чи товар, за яким стежимо для перепродажу. Технічний механізм
        # той самий (стеження за URL), різниця лише в тому, як показуємо.
        "own_listing": own_listing,
        "created_at": now,
    }
    result = await db_call(olx_tracked_col.insert_one(doc))
    return str(result.inserted_id)


async def add_search_tracker(uid: int, title_query: str, max_price: float | None, location: str, radius_km: int) -> str:
    doc = {
        "uid": uid,
        "type": "search",
        "title_query": title_query,
        "max_price": max_price,
        "location": location,
        "radius_km": radius_km,
        "seen_ids": [],
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(olx_tracked_col.insert_one(doc))
    return str(result.inserted_id)


async def add_scanner_tracker(
    uid: int,
    query: str,
    max_price: float | None,
    location: str,
    radius_km: int,
) -> str:
    """
    НОВЕ: 🧠 AI Scanner — підписка на періодичний AI-скан нових оголошень
    за критеріями (на відміну від "search"-трекера, тут кожен новий
    кандидат прожовується через AI resale-аналіз, а не просто показується).
    Свідомо збережено в тій самій колекції olx_tracked_col (type="scanner"),
    щоб перевикористати весь наявний механізм ітерації/видалення трекерів
    замість дублювання CRUD-функцій.
    """
    doc = {
        "uid": uid,
        "type": "scanner",
        "title_query": query,
        "max_price": max_price,
        "location": location,
        "radius_km": radius_km,
        "seen_ids": [],
        "last_checked_at": None,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(olx_tracked_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_trackers(uid: int) -> list[dict]:
    cursor = olx_tracked_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_own_listings(uid: int) -> list[dict]:
    """НОВЕ: список власних оголошень користувача (📋 Мої оголошення)."""
    cursor = olx_tracked_col.find({"uid": uid, "type": "listing", "own_listing": True})
    return await db_call(cursor.to_list(length=100))


async def get_tracker(tracker_id) -> dict | None:
    return await db_call(olx_tracked_col.find_one({"_id": ObjectId(tracker_id)}))


async def get_all_trackers() -> list[dict]:
    cursor = olx_tracked_col.find({})
    return await db_call(cursor.to_list(length=1000))


async def update_listing_price(tracker_id, new_price: float, currency: str | None = None):
    """
    ЗМІНЕНО: тепер, окрім last_price, дописує запис у price_history —
    це і є джерело даних для 📉 Історія зниження ціни (тільки з моменту
    додавання в моніторинг, як і вимагалось).
    """
    entry = {"price": new_price, "currency": currency, "at": datetime.now().isoformat()}
    update = {"$set": {"last_price": new_price}, "$push": {"price_history": entry}}
    if currency:
        update["$set"]["currency"] = currency
    await db_call(olx_tracked_col.update_one({"_id": ObjectId(tracker_id)}, update))


async def update_search_seen_ids(tracker_id, seen_ids: list[str]):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"seen_ids": seen_ids}},
        )
    )


async def update_scanner_state(tracker_id, seen_ids: list[str]):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"seen_ids": seen_ids, "last_checked_at": datetime.now().isoformat()}},
        )
    )


def compute_content_hash(tracker: dict) -> str:
    """Публічна обгортка для handlers/olx.py — порахувати актуальний хеш трекера."""
    return _content_hash(
        tracker.get("title"), tracker.get("last_price"),
        tracker.get("description"), tracker.get("photos"),
    )


def price_drop_summary(tracker: dict) -> dict | None:
    """
    НОВЕ: підсумок історії ціни для 📉 Історія зниження ціни.
    Повертає None, якщо в трекера ще менше 2 зафіксованих цін (нема динаміки).
    """
    history = tracker.get("price_history") or []
    prices = [h["price"] for h in history if h.get("price") is not None]
    if len(prices) < 2:
        return None
    first, last = prices[0], prices[-1]
    drop_percent = round((first - last) / first * 100, 1) if first else 0
    return {"prices": prices, "first": first, "last": last, "drop_percent": drop_percent}


async def save_resale_analysis(tracker_id, analysis: dict, content_hash: str):
    """
    Зберігає повний AI-аналіз (resale_engine.analyze_listing) разом з хешем
    контенту, на якому він базувався.
    """
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {
                "resale_analysis": analysis,
                "resale_analysis_at": datetime.now().isoformat(),
                "content_hash": content_hash,
            }},
        )
    )


async def save_negotiation_messages(tracker_id, messages: dict):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"negotiation_messages": messages}},
        )
    )


async def set_tracker_status(tracker_id, status: str):
    """status: watching | bought | sold"""
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"status": status}},
        )
    )


async def set_favorite(tracker_id, value: bool):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"favorited": value}},
        )
    )


async def delete_tracker(tracker_id, uid: int) -> bool:
    result = await db_call(
        olx_tracked_col.delete_one({"_id": ObjectId(tracker_id), "uid": uid})
    )
    return result.deleted_count > 0


# =========================================================
# Resale History / User Settings
# =========================================================

async def get_user_settings(uid: int) -> dict:
    doc = await db_call(olx_user_settings_col.find_one({"uid": uid}), default=None, raise_on_fail=False)
    return doc or {"uid": uid, "budget": None, "min_margin_percent": None}


async def set_user_settings(uid: int, **fields):
    await db_call(
        olx_user_settings_col.update_one(
            {"uid": uid}, {"$set": fields}, upsert=True,
        )
    )


async def add_deal_record(uid: int, tracker_id, deal: dict):
    """
    deal: {item, buy_price, buy_date, costs, sell_price, sell_date,
           profit, roi_percent, days_to_sell, predicted_sell_price}
    """
    doc = {"uid": uid, "tracker_id": str(tracker_id), **deal, "created_at": datetime.now().isoformat()}
    result = await db_call(olx_deals_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_deals(uid: int) -> list[dict]:
    cursor = olx_deals_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=500))


# =========================================================
# 🔥 OLX Тренди — знімки статистики автопошуків
# =========================================================
# ВАЖЛИВО: тут НІЧОГО не вигадується. Кожен запис — це реальний результат
# однієї перевірки одного автопошуку (search-трекера) в scheduler/olx_jobs.py:
# скільки оголошень знайдено зараз, скільки з них нові, яка середня ціна.
# Тренд — це порівняння свіжих знімків з попередніми ЗА ЦИМ САМИМ запитом.
# Дані обмежені тим, що реально шукають користувачі бота — це НЕ загальна
# статистика по всьому OLX, і чесно позначається як така в UI.

async def record_search_stat(title_query: str, domain: str, count_total: int, count_new: int, avg_price: float | None, currency: str):
    doc = {
        "title_query_norm": title_query.strip().lower(),
        "title_query": title_query,
        "domain": domain,
        "count_total": count_total,
        "count_new": count_new,
        "avg_price": avg_price,
        "currency": currency,
        "checked_at": datetime.now().isoformat(),
    }
    await db_call(olx_search_stats_col.insert_one(doc), raise_on_fail=False)


async def get_trend_for_query(title_query: str, days: int = 14) -> dict | None:
    """Порівнює перший і останній знімок за останні `days` днів для конкретного запиту."""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cursor = olx_search_stats_col.find(
        {"title_query_norm": title_query.strip().lower(), "checked_at": {"$gte": since}}
    ).sort("checked_at", 1)
    points = await db_call(cursor.to_list(length=500), default=[], raise_on_fail=False) or []
    if len(points) < 2:
        return None

    first, last = points[0], points[-1]
    count_delta = last["count_total"] - first["count_total"]
    price_delta_percent = None
    if first.get("avg_price") and last.get("avg_price"):
        price_delta_percent = round((last["avg_price"] - first["avg_price"]) / first["avg_price"] * 100, 1)

    return {
        "title_query": last["title_query"],
        "points_count": len(points),
        "count_first": first["count_total"],
        "count_last": last["count_total"],
        "count_delta": count_delta,
        "avg_price_first": first.get("avg_price"),
        "avg_price_last": last.get("avg_price"),
        "price_delta_percent": price_delta_percent,
        "currency": last.get("currency"),
    }


async def get_tracked_query_names(limit: int = 20) -> list[str]:
    """Унікальні нормалізовані запити, за якими взагалі є статистика — щоб не рахувати тренд наосліп."""
    cursor = olx_search_stats_col.aggregate([
        {"$group": {"_id": "$title_query_norm", "title_query": {"$last": "$title_query"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 2}}},
        {"$limit": limit},
    ])
    docs = await db_call(cursor.to_list(length=limit), default=[], raise_on_fail=False) or []
    return [d["title_query"] for d in docs]