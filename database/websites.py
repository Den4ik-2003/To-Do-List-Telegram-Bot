"""
НОВИЙ ФАЙЛ: database/websites.py

Зберігає згенеровані AI Website Builder сайти: files-знімок (щоб можна
було "Переробити" навіть після рестарту бота), GitHub-репозиторій (якщо
задеплоєно) і Netlify-сайт (якщо задеплоєно).

Використовує ТОЙ САМИЙ підхід, що й решта database/*.py в цьому проєкті:
модульна глобальна колекція з database/mongo.py (websites_col), а не
окрема функція підключення. Індекс (uid, updatedAt) створюється в
mongo.py -> _ensure_websites_indexes() при старті бота.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId

from database import mongo

logger = logging.getLogger("tasks_bot")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_website(uid: int, data: dict) -> str:
    doc = {
        "uid": uid,
        "siteName": data.get("site_name"),
        "summary": data.get("summary"),
        "githubOwner": data.get("github_owner"),
        "githubRepo": data.get("github_repo"),
        "branch": data.get("branch"),
        "netlifySiteId": data.get("netlify_site_id"),
        "netlifyUrl": data.get("netlify_url"),
        "files": data.get("files", {}),
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    result = await mongo.websites_col.insert_one(doc)
    return str(result.inserted_id)


async def get_website(uid: int, site_id: str) -> dict | None:
    try:
        oid = ObjectId(site_id)
    except Exception:
        return None
    doc = await mongo.websites_col.find_one({"_id": oid, "uid": uid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def get_user_websites(uid: int, limit: int = 20) -> list[dict]:
    cursor = mongo.websites_col.find({"uid": uid}).sort("updatedAt", -1).limit(limit)
    out = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


async def update_website(uid: int, site_id: str, patch: dict) -> bool:
    try:
        oid = ObjectId(site_id)
    except Exception:
        return False
    patch = {**patch, "updatedAt": _now()}
    result = await mongo.websites_col.update_one({"_id": oid, "uid": uid}, {"$set": patch})
    return result.matched_count > 0


async def delete_website(uid: int, site_id: str) -> bool:
    try:
        oid = ObjectId(site_id)
    except Exception:
        return False
    result = await mongo.websites_col.delete_one({"_id": oid, "uid": uid})
    return result.deleted_count > 0

# додати в кінець файлу
async def get_website_by_id_any_owner(site_id: str) -> dict | None:
    """Використовується ЛИШЕ webhook'ом /order/{site_id} у main.py — там
    ще невідомо, хто власник (форма шле лише site_id), тому пошук іде без
    фільтра по uid. Усі інші місця в коді (handlers/website_builder.py)
    продовжують використовувати get_website(uid, site_id) з перевіркою
    власника — цей метод для них НЕ підходить і не повинен використовуватись."""
    try:
        oid = ObjectId(site_id)
    except Exception:
        return None
    doc = await mongo.websites_col.find_one({"_id": oid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc