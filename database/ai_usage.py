from datetime import datetime

from database.mongo import ai_usage_col, db_call


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def get_usage(uid: int) -> dict:
    today = _today()
    doc = await db_call(ai_usage_col.find_one({"uid": uid, "date": today}), raise_on_fail=False)
    if not doc:
        return {"uid": uid, "date": today, "count": 0}
    return doc


async def increment_usage(uid: int) -> int:
    today = _today()
    doc = await db_call(
        ai_usage_col.find_one_and_update(
            {"uid": uid, "date": today},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=True,
        )
    )
    return doc["count"]


async def get_remaining(uid: int, daily_limit: int) -> int:
    usage = await get_usage(uid)
    return max(0, daily_limit - usage.get("count", 0))


async def can_use_ai(uid: int, daily_limit: int) -> bool:
    remaining = await get_remaining(uid, daily_limit)
    return remaining > 0