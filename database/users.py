import logging

from database import mongo as m
from database.mongo import db_call

logger = logging.getLogger("tasks_bot")

# Кеш авторизованих user_id, щоб не ходити в Mongo на кожне повідомлення
authorized_uids: set[int] = set()


async def is_authorized(uid: int) -> bool:
    if uid in authorized_uids:
        return True
    doc = await db_call(m.auth_col.find_one({"uid": uid}))
    if doc is not None:
        authorized_uids.add(uid)
        return True
    return False


async def authorize(uid: int):
    authorized_uids.add(uid)
    await db_call(m.auth_col.update_one({"uid": uid}, {"$set": {"uid": uid}}, upsert=True))


async def load_authorized_uids():
    cursor = m.auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    for d in docs:
        if "uid" in d:
            authorized_uids.add(d["uid"])
    logger.info("Loaded %d authorized users into cache", len(authorized_uids))


async def get_all_uids() -> list:
    if authorized_uids:
        return list(authorized_uids)
    cursor = m.auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    return [d["uid"] for d in docs if "uid" in d]


async def get_user_state(uid: int) -> dict:
    doc = await db_call(m.users_col.find_one({"uid": uid}))
    if not doc:
        return {
            "uid": uid, "streak": 0, "last_streak_date": "",
            "archive_prompt_month": "", "xp": 0,
            "total_completed": 0, "total_missed": 0, "total_postponed": 0,
            "last_ai_plan_date": "", "ai_morning_enabled": True,
        }
    doc.setdefault("xp", 0)
    doc.setdefault("total_completed", 0)
    doc.setdefault("total_missed", 0)
    doc.setdefault("total_postponed", 0)
    doc.setdefault("last_ai_plan_date", "")
    doc.setdefault("ai_morning_enabled", True)
    return doc


async def save_user_state(uid: int, fields: dict):
    await db_call(m.users_col.update_one({"uid": uid}, {"$set": fields}, upsert=True))


async def update_streak(uid: int, missed_count: int) -> int:
    """
    Оновлює денну серію (streak) користувача. Викликається раз на день
    (у вечірньому звіті) — якщо за день не було жодної пропущеної задачі,
    серія зростає, інакше скидається в 0.
    """
    from datetime import datetime as _dt

    state = await get_user_state(uid)
    today_str = _dt.now().strftime("%d.%m.%Y")
    if state.get("last_streak_date") == today_str:
        return state.get("streak", 0)

    streak = state.get("streak", 0)
    if missed_count == 0:
        streak += 1
    else:
        streak = 0
    await save_user_state(uid, {"streak": streak, "last_streak_date": today_str})
    return streak