from datetime import datetime

from database.mongo import users_col, auth_col, db_call

authorized_uids: set[int] = set()


async def is_authorized(uid: int) -> bool:
    if uid in authorized_uids:
        return True
    doc = await db_call(auth_col.find_one({"uid": uid}))
    if doc is not None:
        authorized_uids.add(uid)
        return True
    return False


async def authorize(uid: int):
    authorized_uids.add(uid)
    await db_call(auth_col.update_one({"uid": uid}, {"$set": {"uid": uid}}, upsert=True))


async def load_authorized_uids():
    cursor = auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    for d in docs:
        if "uid" in d:
            authorized_uids.add(d["uid"])


async def get_all_uids() -> list:
    if authorized_uids:
        return list(authorized_uids)
    cursor = auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    return [d["uid"] for d in docs if "uid" in d]


def _default_user_state(uid: int) -> dict:
    return {
        "uid": uid,
        "streak": 0,
        "last_streak_date": "",
        "archive_prompt_month": "",
        "xp": 0,
        "total_completed": 0,
        "total_missed": 0,
        "total_postponed": 0,
        "last_ai_plan_date": "",
        "ai_morning_enabled": True,
        "ai_evening_enabled": True,
        "currency": "грн",
        "morning_plan_time": "",
    }


async def get_user_state(uid: int) -> dict:
    doc = await db_call(users_col.find_one({"uid": uid}))
    defaults = _default_user_state(uid)
    if not doc:
        return defaults
    for key, value in defaults.items():
        doc.setdefault(key, value)
    return doc


async def save_user_state(uid: int, fields: dict):
    await db_call(users_col.update_one({"uid": uid}, {"$set": fields}, upsert=True))


async def get_streak(uid: int) -> int:
    state = await get_user_state(uid)
    return state.get("streak", 0)


async def update_streak(uid: int, missed_count: int) -> int:
    state = await get_user_state(uid)
    today_str = datetime.now().strftime("%d.%m.%Y")
    if state.get("last_streak_date") == today_str:
        return state.get("streak", 0)
    streak = state.get("streak", 0)
    if missed_count == 0:
        streak += 1
    else:
        streak = 0
    await save_user_state(uid, {"streak": streak, "last_streak_date": today_str})
    return streak