from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call
from config.constants import GOAL_ACTIVE


async def get_active_goals(uid: int) -> list:
    cursor = m.goals_col.find({"uid": uid, "status": GOAL_ACTIVE})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_active_financial_goals(uid: int) -> list:
    cursor = m.goals_col.find({"uid": uid, "status": GOAL_ACTIVE, "goal_type": "financial"})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_goals(uid: int) -> list:
    cursor = m.goals_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_goal(gid: str) -> dict | None:
    return await db_call(m.goals_col.find_one({"_id": ObjectId(gid)}), raise_on_fail=False)


async def add_goal(
    uid: int,
    title: str,
    description: str,
    priority: str,
    goal_type: str = "general",
    target_amount: float | None = None,
) -> str:
    doc = {
        "uid": uid,
        "title": title,
        "description": description,
        "priority": priority,
        "status": GOAL_ACTIVE,
        "goal_type": goal_type,
        "target_amount": target_amount,
        "current_amount": 0 if goal_type == "financial" else None,
        "deadline": None,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.goals_col.insert_one(doc))
    return str(result.inserted_id)


async def set_goal_status(gid: str, status: str):
    await db_call(m.goals_col.update_one({"_id": ObjectId(gid)}, {"$set": {"status": status}}))


async def update_goal_progress(gid: str, delta_amount: float):
    await db_call(m.goals_col.update_one({"_id": ObjectId(gid)}, {"$inc": {"current_amount": delta_amount}}))


async def delete_goal(gid: str):
    await db_call(m.goals_col.delete_one({"_id": ObjectId(gid)}))