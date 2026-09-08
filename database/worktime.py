from datetime import datetime

from database import mongo as m
from database.mongo import db_call


async def upsert_entry(uid: int, date_str: str, hours: float) -> None:
    now_iso = datetime.now().isoformat()
    await db_call(
        m.worktime_col.update_one(
            {"uid": uid, "date": date_str},
            {
                "$set": {"hours": hours, "updated_at": now_iso},
                "$setOnInsert": {"uid": uid, "date": date_str, "created_at": now_iso},
            },
            upsert=True,
        )
    )


async def get_entry(uid: int, date_str: str) -> dict | None:
    return await db_call(m.worktime_col.find_one({"uid": uid, "date": date_str}, {"_id": 0}))


async def delete_entry(uid: int, date_str: str) -> None:
    await db_call(m.worktime_col.delete_one({"uid": uid, "date": date_str}))


async def get_entries_range(uid: int, start_date: str, end_date: str) -> list:
    cursor = m.worktime_col.find(
        {"uid": uid, "date": {"$gte": start_date, "$lte": end_date}},
        {"_id": 0},
    )
    entries = await db_call(cursor.to_list(length=None), default=[]) or []
    return sorted(entries, key=lambda e: e["date"])


async def get_recent_entries(uid: int, limit: int = 60) -> list:
    cursor = m.worktime_col.find({"uid": uid}, {"_id": 0}).sort("date", -1).limit(limit)
    return await db_call(cursor.to_list(length=None), default=[]) or []


async def has_entry_for_date(uid: int, date_str: str) -> bool:
    entry = await get_entry(uid, date_str)
    return entry is not None