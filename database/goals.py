from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


async def get_active_goals(uid: int) -> list:
    cursor = m.goals_col.find({"uid": uid, "active": True})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_goals(uid: int) -> list:
    cursor = m.goals_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def add_goal(uid: int, title: str, description: str, priority: str):
    await db_call(m.goals_col.insert_one({
        "uid": uid,
        "title": title,
        "description": description,
        "priority": priority,
        "active": True,
        "created_at": datetime.now().isoformat(),
    }))


async def toggle_goal(gid: str, active: bool):
    await db_call(m.goals_col.update_one({"_id": ObjectId(gid)}, {"$set": {"active": active}}))


async def delete_goal(gid: str):
    await db_call(m.goals_col.delete_one({"_id": ObjectId(gid)}))