import logging
from datetime import datetime, timezone

from bson import ObjectId

from database import mongo

logger = logging.getLogger("tasks_bot")

MAX_VERSIONS = 5


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
        "versions": [],
        "checklist": data.get("checklist", []),
        "notifyBotToken": None,
        "notifyChatId": None,
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


async def get_website_by_id_any_owner(site_id: str) -> dict | None:
    try:
        oid = ObjectId(site_id)
    except Exception:
        return None
    doc = await mongo.websites_col.find_one({"_id": oid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def save_version(uid: int, site_id: str, version: dict) -> bool:
    try:
        oid = ObjectId(site_id)
    except Exception:
        return False
    entry = {
        "files": version.get("files", {}),
        "summary": version.get("summary", ""),
        "commitMessage": version.get("commit_message", ""),
        "savedAt": _now(),
    }
    result = await mongo.websites_col.update_one(
        {"_id": oid, "uid": uid},
        {"$push": {"versions": {"$each": [entry], "$position": 0, "$slice": MAX_VERSIONS}}},
    )
    return result.matched_count > 0


async def get_versions(uid: int, site_id: str) -> list[dict]:
    doc = await get_website(uid, site_id)
    if not doc:
        return []
    return doc.get("versions", [])


async def save_checklist(uid: int, site_id: str, checklist: list[str]) -> bool:
    return await update_website(uid, site_id, {"checklist": checklist})


async def set_notification_bot(uid: int, site_id: str, encrypted_token: str | None, chat_id: str | None) -> bool:
    return await update_website(uid, site_id, {"notifyBotToken": encrypted_token, "notifyChatId": chat_id})


async def get_notification_config(site_id: str) -> dict | None:
    doc = await get_website_by_id_any_owner(site_id)
    if not doc:
        return None
    return {
        "uid": doc["uid"],
        "bot_token_encrypted": doc.get("notifyBotToken"),
        "chat_id": doc.get("notifyChatId"),
        "site_name": doc.get("siteName"),
    }