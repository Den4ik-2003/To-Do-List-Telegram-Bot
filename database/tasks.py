from database import mongo as m
from database.mongo import db_call
from config.constants import LABEL_ORDER


async def next_task_id() -> int:
    doc = await db_call(
        m.counters_col.find_one_and_update(
            {"_id": "task_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
    )
    return doc["seq"]


async def add_task(task: dict):
    await db_call(m.tasks_col.insert_one(task))


async def get_task(tid: int) -> dict | None:
    return await db_call(m.tasks_col.find_one({"id": tid}, {"_id": 0}))


async def update_task(tid: int, fields: dict):
    await db_call(m.tasks_col.update_one({"id": tid}, {"$set": fields}))


async def delete_task(tid: int):
    await db_call(m.tasks_col.delete_one({"id": tid}))


async def get_user_tasks(uid: int, statuses: list | None = None) -> list:
    query = {"uid": uid}
    if statuses:
        query["status"] = {"$in": statuses}
    cursor = m.tasks_col.find(query, {"_id": 0})
    tasks = await db_call(cursor.to_list(length=None), default=[]) or []
    return tasks


async def find_pending(extra_filter: dict | None = None) -> list:
    """Використовується фоновими job'ами (нагадування, rollover)."""
    query = {"status": "pending"}
    if extra_filter:
        query.update(extra_filter)
    cursor = m.tasks_col.find(query, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


def sort_tasks(tasks: list) -> list:
    def key(t):
        return (t.get("due", ""), LABEL_ORDER.get(t.get("label", "idea"), 9))
    return sorted(tasks, key=key)


def sort_tasks_by_label_then_due(tasks: list) -> list:
    def key(t):
        return (LABEL_ORDER.get(t.get("label", "idea"), 9), t.get("due", ""))
    return sorted(tasks, key=key)