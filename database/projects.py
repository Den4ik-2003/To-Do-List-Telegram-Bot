from datetime import datetime

from bson import ObjectId

from database.mongo import projects_col, db_call

PROJECT_ACTIVE = "active"
PROJECT_DONE = "done"


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
        "deadline": deadline,
        "budget": budget,
        "spent": 0.0,
        "goal_id": goal_id,
    }
    result = await db_call(projects_col.insert_one(doc))
    return str(result.inserted_id)


async def get_project(pid: str) -> dict | None:
    return await db_call(projects_col.find_one({"_id": ObjectId(pid)}))


async def get_active_projects(uid: int) -> list:
    cursor = projects_col.find({"uid": uid, "status": PROJECT_ACTIVE}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_projects(uid: int) -> list:
    cursor = projects_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def update_project(pid: str, fields: dict):
    await db_call(projects_col.update_one({"_id": ObjectId(pid)}, {"$set": fields}))


async def set_project_status(pid: str, status: str):
    await db_call(projects_col.update_one({"_id": ObjectId(pid)}, {"$set": {"status": status}}))


async def increment_project_spent(pid: str, amount: float):
    await db_call(projects_col.update_one({"_id": ObjectId(pid)}, {"$inc": {"spent": amount}}))


async def delete_project(pid: str):
    await db_call(projects_col.delete_one({"_id": ObjectId(pid)}))