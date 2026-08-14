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


async def add_project(uid: int, title: str, description: str):
    await db_call(m.projects_col.insert_one({
        "uid": uid,
        "title": title,
        "description": description,
        "status": PROJECT_ACTIVE,
        "created_at": datetime.now().isoformat(),
    }))


async def set_project_status(pid: str, status: str):
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$set": {"status": status}}))


async def delete_project(pid: str):
    await db_call(m.projects_col.delete_one({"_id": ObjectId(pid)}))