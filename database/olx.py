from bson import ObjectId
from datetime import datetime

from database.mongo import olx_tracked_col, db_call


async def add_listing_tracker(uid: int, url: str, last_price: float | None, currency: str) -> str:
    doc = {
        "uid": uid,
        "type": "listing",
        "url": url,
        "last_price": last_price,
        "currency": currency,
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


async def delete_tracker(tracker_id, uid: int) -> bool:
    result = await db_call(
        olx_tracked_col.delete_one({"_id": ObjectId(tracker_id), "uid": uid})
    )
    return result.deleted_count > 0