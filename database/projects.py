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
# Порядковий номер етапу = його позиція в масиві "stages" (позиція+1), окреме
# поле для номера навмисно не заводимо — це прибирає ризик розсинхронізації
# двох джерел правди при зміні порядку.
#
# "Статус" в UI має 3 стани (⏳ Очікує / 🔵 В процесі / ✅ Завершено), але в базі
# зберігаємо тільки 2 ("pending"/"done") — так само, як і раніше. "В процесі"
# обчислюється на льоту: це ПЕРШИЙ pending-етап за порядком (get_current_stage).
# Так гарантовано лише один етап "в процесі" одночасно, без нового поля.

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


async def add_stages_bulk(pid: str, stages: list[dict]) -> None:
    """Додає одразу декілька етапів — використовується після підтвердження AI-пропозиції."""
    prepared = []
    for s in stages:
        prepared.append({
            "id": uuid.uuid4().hex[:8],
            "title": (s.get("title") or "")[:100],
            "description": (s.get("description") or "")[:400],
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        })
    if not prepared:
        return
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$push": {"stages": {"$each": prepared}}}))


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


async def edit_stage(pid: str, stage_id: str, title: str | None = None, description: str | None = None):
    """None означає «не змінювати це поле» (на відміну від "" — порожній рядок)."""
    fields = {}
    if title is not None:
        fields["title"] = title
    if description is not None:
        fields["description"] = description
    if fields:
        await update_stage(pid, stage_id, fields)


async def delete_stage(pid: str, stage_id: str):
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$pull": {"stages": {"id": stage_id}}}))


async def reorder_stage(pid: str, stage_id: str, direction: str) -> bool:
    """direction: 'up' або 'down'. Міняє етап місцями із сусіднім у масиві.
    Повертає True, якщо переміщення відбулось (False — якщо етап уже крайній)."""
    p = await get_project(pid)
    if not p:
        return False
    stages = list(p.get("stages") or [])
    idx = next((i for i, s in enumerate(stages) if s.get("id") == stage_id), None)
    if idx is None:
        return False
    if direction == "up" and idx > 0:
        stages[idx - 1], stages[idx] = stages[idx], stages[idx - 1]
    elif direction == "down" and idx < len(stages) - 1:
        stages[idx + 1], stages[idx] = stages[idx], stages[idx + 1]
    else:
        return False
    await db_call(m.projects_col.update_one({"_id": ObjectId(pid)}, {"$set": {"stages": stages}}))
    return True


def get_current_stage(project: dict) -> dict | None:
    """Перший ще не завершений етап вважається поточним активним етапом проєкту (🔵 В процесі)."""
    stages = project.get("stages") or []
    for s in stages:
        if s.get("status") != "done":
            return s
    return None


def stage_progress(project: dict) -> tuple[int, int]:
    stages = project.get("stages") or []
    done = sum(1 for s in stages if s.get("status") == "done")
    return done, len(stages)