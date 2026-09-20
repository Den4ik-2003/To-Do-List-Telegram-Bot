

import logging
from datetime import datetime, timezone

from bson import ObjectId

from database import mongo

logger = logging.getLogger("tasks_bot")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_template(uid: int, name: str, files: dict[str, str]) -> str:
    doc = {
        "uid": uid,
        "name": name,
        "files": files,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    result = await mongo.templates_col.insert_one(doc)
    return str(result.inserted_id)


async def get_template(uid: int, template_id: str) -> dict | None:
    try:
        oid = ObjectId(template_id)
    except Exception:
        return None
    doc = await mongo.templates_col.find_one({"_id": oid, "uid": uid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def get_user_templates(uid: int, limit: int = 20) -> list[dict]:
    cursor = mongo.templates_col.find({"uid": uid}).sort("updatedAt", -1).limit(limit)
    out = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


async def rename_template(uid: int, template_id: str, name: str) -> bool:
    try:
        oid = ObjectId(template_id)
    except Exception:
        return False
    result = await mongo.templates_col.update_one(
        {"_id": oid, "uid": uid}, {"$set": {"name": name, "updatedAt": _now()}},
    )
    return result.matched_count > 0


async def delete_template(uid: int, template_id: str) -> bool:
    try:
        oid = ObjectId(template_id)
    except Exception:
        return False
    result = await mongo.templates_col.delete_one({"_id": oid, "uid": uid})
    return result.deleted_count > 0