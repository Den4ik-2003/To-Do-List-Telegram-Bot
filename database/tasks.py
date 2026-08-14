from database.mongo import tasks_col, counters_col, db_call


async def next_task_id() -> int:
    doc = await db_call(
        counters_col.find_one_and_update(
            {"_id": "task_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
    )
    return doc["seq"]


async def add_task(task: dict):
    await db_call(tasks_col.insert_one(task))


async def get_task(tid: int) -> dict | None:
    return await db_call(tasks_col.find_one({"id": tid}, {"_id": 0}))


async def update_task(tid: int, fields: dict):
    await db_call(tasks_col.update_one({"id": tid}, {"$set": fields}))


async def delete_task(tid: int):
    await db_call(tasks_col.delete_one({"id": tid}))


async def get_user_tasks(uid: int, statuses: list | None = None) -> list:
    query = {"uid": uid}
    if statuses:
        query["status"] = {"$in": statuses}
    cursor = tasks_col.find(query, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[]) or []


async def get_project_tasks(uid: int, project_id: str, statuses: list | None = None) -> list:
    query = {"uid": uid, "project_id": project_id}
    if statuses:
        query["status"] = {"$in": statuses}
    cursor = tasks_col.find(query, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[]) or []


async def get_pending_with_reminder_due() -> list:
    cursor = tasks_col.find({"status": "pending", "reminded_before": False}, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_all_pending() -> list:
    cursor = tasks_col.find({"status": "pending"}, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_postponed_today_ids(uid: int) -> list:
    cursor = tasks_col.find({"uid": uid, "postponed_today": True}, {"_id": 0, "id": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    return [d["id"] for d in docs]