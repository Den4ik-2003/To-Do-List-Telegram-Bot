from datetime import datetime

from bson import ObjectId

from database.mongo import goals_col, db_call

GOAL_ACTIVE = "active"
GOAL_DONE = "done"

GOAL_TYPE_FINANCIAL = "financial"
GOAL_TYPE_GENERAL = "general"


async def add_goal(
    uid: int,
    title: str,
    description: str = "",
    priority: str = "medium",
    goal_type: str = GOAL_TYPE_GENERAL,
    target_amount: float | None = None,
    current_amount: float = 0.0,
    deadline: str | None = None,
) -> str:
    doc = {
        "uid": uid,
        "title": title,
        "description": description,
        "priority": priority,
        "goal_type": goal_type,
        "target_amount": target_amount,
        "current_amount": current_amount,
        "deadline": deadline,
        "status": GOAL_ACTIVE,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(goals_col.insert_one(doc))
    return str(result.inserted_id)


async def get_goal(gid: str) -> dict | None:
    return await db_call(goals_col.find_one({"_id": ObjectId(gid)}))


async def get_active_goals(uid: int) -> list:
    cursor = goals_col.find({"uid": uid, "status": GOAL_ACTIVE}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_goals(uid: int) -> list:
    cursor = goals_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def update_goal(gid: str, fields: dict):
    await db_call(goals_col.update_one({"_id": ObjectId(gid)}, {"$set": fields}))


async def set_goal_status(gid: str, status: str):
    await db_call(goals_col.update_one({"_id": ObjectId(gid)}, {"$set": {"status": status}}))


async def toggle_goal(gid: str, active: bool):
    await set_goal_status(gid, GOAL_ACTIVE if active else GOAL_DONE)


async def add_goal_progress(gid: str, amount: float):
    await db_call(goals_col.update_one({"_id": ObjectId(gid)}, {"$inc": {"current_amount": amount}}))


async def delete_goal(gid: str):
    await db_call(goals_col.delete_one({"_id": ObjectId(gid)}))


async def get_active_financial_goals(uid: int) -> list:
    cursor = goals_col.find({"uid": uid, "status": GOAL_ACTIVE, "goal_type": GOAL_TYPE_FINANCIAL}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []