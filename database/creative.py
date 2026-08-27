import datetime
import uuid

from database.mongo import creative_generations_col, db_call
from config.settings import CREATIVE_DAILY_LIMIT


async def save_generation(uid: int, kind: str, prompt: str, file_id: str, meta: dict | None = None) -> str:
    gen_id = str(uuid.uuid4())
    doc = {
        "_id": gen_id,
        "uid": uid,
        "kind": kind,  # generate | edit | sticker | emoji | template
        "prompt": prompt,
        "file_id": file_id,
        "meta": meta or {},
        "created_at": datetime.datetime.utcnow(),
    }
    await db_call(creative_generations_col.insert_one(doc))
    return gen_id


async def get_user_generations(uid: int, limit: int = 10, skip: int = 0) -> list[dict]:
    cursor = (
        creative_generations_col.find({"uid": uid})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    return await db_call(cursor.to_list(length=limit), default=[])


async def count_today(uid: int) -> int:
    start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return await db_call(
        creative_generations_col.count_documents({"uid": uid, "created_at": {"$gte": start}}),
        default=0,
    )


async def check_daily_limit(uid: int) -> bool:
    """True якщо ліміт ще не вичерпано."""
    return await count_today(uid) < CREATIVE_DAILY_LIMIT