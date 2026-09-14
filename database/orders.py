"""
НОВИЙ ФАЙЛ: database/orders.py

Замовлення, що надходять з форм на згенерованих AI Website Builder
сайтах (webhook POST /order/{site_id} у main.py). Кожен запис прив'язаний
до site_id (з database/websites.py) і до owner_uid — власника сайту, щоб
"🌐 Мої сайти → [сайт] → 📦 Замовлення" показувало ЛИШЕ замовлення цього
сайту цьому користувачу.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId

from database import mongo

logger = logging.getLogger("tasks_bot")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_order(site_id: str, owner_uid: int, site_name: str, name: str,
                        phone: str, product: str, comment: str) -> str:
    doc = {
        "site_id": site_id,
        "owner_uid": owner_uid,
        "site_name": site_name,
        "name": (name or "")[:200],
        "phone": (phone or "")[:50],
        "product": (product or "")[:300],
        "comment": (comment or "")[:1000],
        "created_at": _now(),
    }
    result = await mongo.orders_col.insert_one(doc)
    return str(result.inserted_id)


async def get_orders_for_site(owner_uid: int, site_id: str, limit: int = 20) -> list[dict]:
    cursor = mongo.orders_col.find(
        {"site_id": site_id, "owner_uid": owner_uid}
    ).sort("created_at", -1).limit(limit)
    out = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


async def count_orders_for_site(owner_uid: int, site_id: str) -> int:
    return await mongo.orders_col.count_documents({"site_id": site_id, "owner_uid": owner_uid})