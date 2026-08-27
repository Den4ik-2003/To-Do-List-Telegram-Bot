from datetime import datetime

from bson import ObjectId

from database.mongo import job_saved_col, job_searches_col, db_call


async def save_vacancy(uid: int, vacancy: dict) -> str:
    doc = {
        "uid": uid,
        "title": vacancy.get("title"),
        "company": vacancy.get("company"),
        "location": vacancy.get("location"),
        "work_format": vacancy.get("work_format"),
        "salary": vacancy.get("salary"),
        "url": vacancy.get("url"),
        "source": vacancy.get("source"),
        "match_percent": vacancy.get("match_percent"),
        "cover_letter": vacancy.get("cover_letter"),
        "saved_at": datetime.now().isoformat(),
    }
    result = await db_call(job_saved_col.insert_one(doc))
    return str(result.inserted_id)


async def update_cover_letter(saved_id, cover_letter: str):
    await db_call(
        job_saved_col.update_one(
            {"_id": ObjectId(saved_id)}, {"$set": {"cover_letter": cover_letter}}
        )
    )


async def get_saved(uid: int) -> list[dict]:
    cursor = job_saved_col.find({"uid": uid}).sort("saved_at", -1)
    return await db_call(cursor.to_list(length=100))


async def delete_saved(saved_id, uid: int) -> bool:
    result = await db_call(
        job_saved_col.delete_one({"_id": ObjectId(saved_id), "uid": uid})
    )
    return result.deleted_count > 0


async def add_search_watch(uid: int, criteria: dict, seen_ids: list[str]) -> str:
    doc = {
        "uid": uid,
        "criteria": criteria,
        "seen_ids": seen_ids,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(job_searches_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_watches(uid: int) -> list[dict]:
    cursor = job_searches_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=50))


async def get_all_watches() -> list[dict]:
    cursor = job_searches_col.find({})
    return await db_call(cursor.to_list(length=500))


async def update_watch_seen(watch_id, seen_ids: list[str]):
    await db_call(
        job_searches_col.update_one(
            {"_id": ObjectId(watch_id)}, {"$set": {"seen_ids": seen_ids}}
        )
    )


async def delete_watch(watch_id, uid: int) -> bool:
    result = await db_call(
        job_searches_col.delete_one({"_id": ObjectId(watch_id), "uid": uid})
    )
    return result.deleted_count > 0