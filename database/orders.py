import logging
from datetime import datetime, timezone

from bson import ObjectId

from database import mongo

logger = logging.getLogger("tasks_bot")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_order(
    site_id: str, owner_uid: int, site_name: str,
    name: str, phone: str, product: str, comment: str,
) -> str:
    doc = {
        "site_id": site_id,
        "owner_uid": owner_uid,
        "site_name": site_name,
        "name": name,
        "phone": phone,
        "product": product,
        "comment": comment,
        "delivered": False,
        "delivery_attempts": 0,
        "last_error": None,
        "created_at": _now(),
        "delivered_at": None,
    }
    result = await mongo.orders_col.insert_one(doc)
    return str(result.inserted_id)


async def mark_delivered(order_id: str) -> bool:
    try:
        oid = ObjectId(order_id)
    except Exception:
        return False
    result = await mongo.orders_col.update_one(
        {"_id": oid},
        {"$set": {"delivered": True, "delivered_at": _now()}, "$inc": {"delivery_attempts": 1}},
    )
    return result.matched_count > 0


async def mark_delivery_failed(order_id: str, error: str) -> bool:
    try:
        oid = ObjectId(order_id)
    except Exception:
        return False
    result = await mongo.orders_col.update_one(
        {"_id": oid},
        {"$set": {"delivered": False, "last_error": (error or "")[:500]}, "$inc": {"delivery_attempts": 1}},
    )
    return result.matched_count > 0


async def get_orders_for_site(owner_uid: int, site_id: str, limit: int = 20) -> list[dict]:
    cursor = mongo.orders_col.find(
        {"owner_uid": owner_uid, "site_id": site_id}
    ).sort("created_at", -1).limit(limit)
    out = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


async def get_undelivered_orders(limit: int = 50) -> list[dict]:
    cursor = mongo.orders_col.find({"delivered": False}).sort("created_at", -1).limit(limit)
    out = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out