import uuid
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
        "stages": [],
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


# =========================================================
# ЕТАПИ ПРОЄКТУ
# =========================================================
# Кожен етап: {"id": str, "title": str, "description": str, "status": "pending"|"done", "created_at": str}
# Явного поля "поточний етап" немає навмисно — поточним вважається ПЕРШИЙ
# етап зі статусом "pending" (за порядком у масиві). Це прибирає ризик
# розсинхронізації двох джерел правди (окремий "current_stage_id" міг би
# вказувати на вже видалений/завершений етап).

async def add_stage(pid: str, title: str, description: str = "") -> str:
    stage_id = uuid.uuid4().hex[:8]
    stage = {
        "id": stage_id,
        "title": title,
        "description": description,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$push": {"stages": stage}}))
    return stage_id


async def get_stages(pid: str) -> list:
    p = await get_project(pid)
    return (p or {}).get("stages", [])


async def get_stage(pid: str, stage_id: str) -> dict | None:
    stages = await get_stages(pid)
    for s in stages:
        if s.get("id") == stage_id:
            return s
    return None


async def update_stage(pid: str, stage_id: str, fields: dict):
    await db_call(m.projects_col.update_one(
        {"_id": ObjectId(pid), "stages.id": stage_id},
        {"$set": {f"stages.$.{k}": v for k, v in fields.items()}},
    ))


async def delete_stage(pid: str, stage_id: str):
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$pull": {"stages": {"id": stage_id}}}))


def get_current_stage(project: dict) -> dict | None:
    """Перший ще не завершений етап вважається поточним активним етапом проєкту."""
    stages = project.get("stages") or []
    for s in stages:
        if s.get("status") != "done":
            return s
    return None


def stage_progress(project: dict) -> tuple[int, int]:
    stages = project.get("stages") or []
    done = sum(1 for s in stages if s.get("status") == "done")
    return done, len(stages)