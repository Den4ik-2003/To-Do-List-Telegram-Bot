from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call
from config.constants import PROJECT_ACTIVE


async def get_active_projects(uid: int) -> list:
    cursor = m.projects_col.find({"uid": uid, "status": PROJECT_ACTIVE}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_projects(uid: int) -> list:
    cursor = m.projects_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_project(pid: str) -> dict | None:
    return await db_call(m.projects_col.find_one({"_id": ObjectId(pid)}), default=None, raise_on_fail=False)


async def add_project(
    uid: int,
    title: str,
    description: str = "",
    deadline: str | None = None,
    budget: float | None = None,
    goal_id: str | None = None,
) -> str:
    doc = {
        "uid": uid,
        "title": title,
        "description": description,
        "status": PROJECT_ACTIVE,
        "created_at": datetime.now().isoformat(),
    }
    if deadline:
        doc["deadline"] = deadline
    if budget:
        doc["budget"] = budget
        doc["spent"] = 0
    if goal_id:
        doc["goal_id"] = goal_id

    result = await db_call(m.projects_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def set_project_status(pid: str, status: str):
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$set": {"status": status}}))


async def delete_project(pid: str):
    await db_call(m.projects_col.delete_one({"_id": ObjectId(pid)}))