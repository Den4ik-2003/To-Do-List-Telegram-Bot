from datetime import datetime
from collections import Counter

from bson import ObjectId

from database.mongo import job_saved_col, job_searches_col, job_feedback_col, db_call


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
        "sources": vacancy.get("sources") or [vacancy.get("source")],
        "match_percent": vacancy.get("match_percent"),
        "cover_letter": vacancy.get("cover_letter"),
        "status": "saved",
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


async def update_status(saved_id, uid: int, status: str) -> bool:
    result = await db_call(
        job_saved_col.update_one(
            {"_id": ObjectId(saved_id), "uid": uid}, {"$set": {"status": status}}
        )
    )
    return result.modified_count > 0


async def get_saved(uid: int) -> list[dict]:
    cursor = job_saved_col.find({"uid": uid}).sort("saved_at", -1)
    return await db_call(cursor.to_list(length=100))


async def is_saved(uid: int, url: str) -> bool:
    doc = await db_call(job_saved_col.find_one({"uid": uid, "url": url}), raise_on_fail=False)
    return bool(doc)


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
        "active": True,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(job_searches_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_watches(uid: int) -> list[dict]:
    cursor = job_searches_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=50))


async def get_all_watches() -> list[dict]:
    cursor = job_searches_col.find({"active": {"$ne": False}})
    return await db_call(cursor.to_list(length=500))


async def update_watch_seen(watch_id, seen_ids: list[str]):
    await db_call(
        job_searches_col.update_one(
            {"_id": ObjectId(watch_id)}, {"$set": {"seen_ids": seen_ids}}
        )
    )


async def set_watch_active(watch_id, uid: int, active: bool) -> bool:
    result = await db_call(
        job_searches_col.update_one(
            {"_id": ObjectId(watch_id), "uid": uid}, {"$set": {"active": active}}
        )
    )
    return result.modified_count > 0


async def delete_watch(watch_id, uid: int) -> bool:
    result = await db_call(
        job_searches_col.delete_one({"_id": ObjectId(watch_id), "uid": uid})
    )
    return result.deleted_count > 0


async def add_feedback(uid: int, vacancy: dict, reason: str):
    doc = {
        "uid": uid,
        "title": vacancy.get("title"),
        "company": vacancy.get("company"),
        "reason": reason,
        "created_at": datetime.now().isoformat(),
    }
    await db_call(job_feedback_col.insert_one(doc))


async def get_recent_feedback(uid: int, limit: int = 10) -> list[dict]:
    cursor = job_feedback_col.find({"uid": uid}).sort("created_at", -1).limit(limit)
    return await db_call(cursor.to_list(length=limit))


async def get_stats(uid: int) -> dict:
    saved = await get_saved(uid)
    status_counts = Counter(v.get("status", "saved") for v in saved)
    matches = [v["match_percent"] for v in saved if v.get("match_percent") is not None]
    companies = Counter(v.get("company") for v in saved if v.get("company"))
    professions = Counter(v.get("title") for v in saved if v.get("title"))
    return {
        "total_saved": len(saved),
        "applied": status_counts.get("applied", 0),
        "response": status_counts.get("response", 0),
        "interview": status_counts.get("interview", 0),
        "hired": status_counts.get("hired", 0),
        "rejected": status_counts.get("rejected", 0),
        "avg_match": round(sum(matches) / len(matches), 1) if matches else None,
        "top_companies": companies.most_common(3),
        "top_titles": professions.most_common(3),
    }