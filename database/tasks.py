"""
ЗМІНЕНИЙ ФАЙЛ: database/tasks.py

Додано:
- get_project_tasks(uid, project_id) — саме її бракувало, через що падало
  відкриття БУДЬ-ЯКОГО проєкту (project_service.get_project_progress його
  викликав, а функції не існувало: AttributeError).
- set_task_project(tid, project_id) — допоміжна функція на майбутнє, щоб
  прив'язати вже існуючу задачу до проєкту (поки що жоден хендлер її не
  викликає — UI для "🔗 Прив'язати задачу до проєкту" ще треба зробити
  окремо, якщо потрібно).

ВАЖЛИВО: у задачах ДОСІ немає поля project_id в жодному вже створеному
документі — його там ніколи не було. Це не міграція, а просто новий
ОПЦІОНАЛЬНИЙ ключ: get_project_tasks фільтрує по ньому, і задачі без
цього поля (тобто буквально всі задачі, створені досі) просто не
потраплять у жоден проєкт. Нічого зі старої поведінки не ламається.
Щоб нові задачі реально прив'язувались до проєкту, треба окремо
допрацювати місце, де задача створюється (найімовірніше handlers/tasks.py
або ai_chat.py) — я його ще не бачив, тож туди нічого не чіпав.

Решта функцій — 1:1 як було.
"""

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


# =========================================================
# НОВЕ: зв'язок задач із проєктами (для 📁 Мої проєкти → прогрес задач)
# =========================================================

async def get_project_tasks(uid: int, project_id: str) -> list:
    """Задачі користувача, прив'язані до конкретного проєкту (поле project_id).
    Ізоляція користувачів зберігається — фільтр по uid обов'язковий, як і
    в усіх інших функціях цього файлу."""
    cursor = m.tasks_col.find({"uid": uid, "project_id": project_id}, {"_id": 0})
    tasks = await db_call(cursor.to_list(length=None), default=[]) or []
    return tasks


async def set_task_project(tid: int, project_id: str | None):
    """Прив'язує (або відв'язує, якщо project_id=None) існуючу задачу до проєкту.
    Поки що не викликається жодним хендлером — заготовка на майбутнє."""
    if project_id:
        await update_task(tid, {"project_id": project_id})
    else:
        await db_call(m.tasks_col.update_one({"id": tid}, {"$unset": {"project_id": ""}}))