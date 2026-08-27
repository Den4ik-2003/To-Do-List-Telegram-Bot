from bson import ObjectId
from datetime import datetime

from database.mongo import site_watch_col, qa_results_col, db_call


async def add_site_watch(uid: int, url: str, label: str = "") -> str:
    doc = {
        "uid": uid,
        "url": url,
        "label": label or url,
        "last_status": None,
        "last_checked_at": None,
        "down_since": None,
        "last_qa_at": None,
        "last_qa_ok": None,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(site_watch_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_watches(uid: int) -> list[dict]:
    cursor = site_watch_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_watch(watch_id) -> dict | None:
    return await db_call(site_watch_col.find_one({"_id": ObjectId(watch_id)}))


async def get_all_watches() -> list[dict]:
    cursor = site_watch_col.find({})
    return await db_call(cursor.to_list(length=1000))


async def update_watch_status(watch_id, is_up: bool, down_since: str | None):
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {
                "last_status": is_up,
                "last_checked_at": datetime.now().isoformat(),
                "down_since": down_since,
            }},
        )
    )


async def save_qa_result(watch_id, uid: int, url: str, report: dict, is_ok: bool) -> str:
    doc = {
        "watch_id": str(watch_id),
        "uid": uid,
        "url": url,
        "report": report,
        "is_ok": is_ok,
        "checked_at": datetime.now().isoformat(),
    }
    result = await db_call(qa_results_col.insert_one(doc))
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {"last_qa_at": doc["checked_at"], "last_qa_ok": is_ok}},
        )
    )
    return str(result.inserted_id)


async def get_last_qa(watch_id) -> dict | None:
    cursor = qa_results_col.find({"watch_id": str(watch_id)}).sort("checked_at", -1).limit(1)
    results = await db_call(cursor.to_list(length=1))
    return results[0] if results else None


async def delete_watch(watch_id, uid: int) -> bool:
    result = await db_call(
        site_watch_col.delete_one({"_id": ObjectId(watch_id), "uid": uid})
    )
    return result.deleted_count > 0