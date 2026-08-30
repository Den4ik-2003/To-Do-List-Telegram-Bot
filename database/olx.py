import hashlib
from bson import ObjectId
from datetime import datetime

from database.mongo import olx_tracked_col, db_call

# Нова колекція для завершених угод (купив -> продав) — Resale History.
try:
    from database.mongo import olx_deals_col, olx_user_settings_col
except ImportError:  # тимчасовий safe-guard, поки колекції не додані в mongo.py
    olx_deals_col = None
    olx_user_settings_col = None


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
) -> str:
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
        # ДОДАНО: окрема позначка "обране" — незалежна від статусу покупки,
        # щоб можна було зберегти цікаве оголошення (п.13/17 ТЗ, кнопка
        # "⭐ Зберегти") навіть якщо його ще не куплено.
        "favorited": False,
        "created_at": datetime.now().isoformat(),
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


async def get_user_trackers(uid: int) -> list[dict]:
    cursor = olx_tracked_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_tracker(tracker_id) -> dict | None:
    return await db_call(olx_tracked_col.find_one({"_id": ObjectId(tracker_id)}))


async def get_all_trackers() -> list[dict]:
    cursor = olx_tracked_col.find({})
    return await db_call(cursor.to_list(length=1000))


async def update_listing_price(tracker_id, new_price: float):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"last_price": new_price}},
        )
    )


async def update_search_seen_ids(tracker_id, seen_ids: list[str]):
    await db_call(
        olx_tracked_col.update_one(
            {"_id": ObjectId(tracker_id)},
            {"$set": {"seen_ids": seen_ids}},
        )
    )


def compute_content_hash(tracker: dict) -> str:
    """Публічна обгортка для handlers/olx.py — порахувати актуальний хеш трекера."""
    return _content_hash(
        tracker.get("title"), tracker.get("last_price"),
        tracker.get("description"), tracker.get("photos"),
    )


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
    """ДОДАНО: перемикач "⭐ Зберегти" — не пов'язаний зі статусом покупки."""
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
    if olx_user_settings_col is None:
        return {"uid": uid, "budget": None, "min_margin_percent": None}
    doc = await db_call(olx_user_settings_col.find_one({"uid": uid}))
    return doc or {"uid": uid, "budget": None, "min_margin_percent": None}


async def set_user_settings(uid: int, **fields):
    if olx_user_settings_col is None:
        return
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
    if olx_deals_col is None:
        return None
    doc = {"uid": uid, "tracker_id": str(tracker_id), **deal, "created_at": datetime.now().isoformat()}
    result = await db_call(olx_deals_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_deals(uid: int) -> list[dict]:
    if olx_deals_col is None:
        return []
    cursor = olx_deals_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=500))