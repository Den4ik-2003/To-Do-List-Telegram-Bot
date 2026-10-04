import re
from datetime import date, datetime, timedelta, time as dtime

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from database import mongo as m
from database.mongo import db_call
from config.constants import LABEL_ORDER, STATUS_PENDING
from utils.dates import (
    DEFAULT_TZ, now_local, local_to_utc, parse_hhmm, next_occurrence, fmt_due,
)


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


async def get_project_tasks(uid: int, project_id: str) -> list:
    cursor = m.tasks_col.find({"uid": uid, "project_id": project_id}, {"_id": 0})
    tasks = await db_call(cursor.to_list(length=None), default=[]) or []
    return tasks


async def set_task_project(tid: int, project_id: str | None):
    if project_id:
        await update_task(tid, {"project_id": project_id})
    else:
        await db_call(m.tasks_col.update_one({"id": tid}, {"$unset": {"project_id": ""}}))


async def get_tasks_due_date(uid: int, date_str: str) -> list:
    cursor = m.tasks_col.find(
        {"uid": uid, "due": {"$regex": f"^{re.escape(date_str)}"}}, {"_id": 0}
    )
    tasks = await db_call(cursor.to_list(length=None), default=[]) or []
    return tasks


def _oid(rid):
    try:
        return ObjectId(rid)
    except (InvalidId, TypeError):
        return None


def _d(value):
    return date.fromisoformat(value) if value else None


def _tz(rec: dict) -> str:
    return (rec.get("schedule") or {}).get("tz") or DEFAULT_TZ


def _run_at_utc(rec: dict, d: date) -> datetime:
    due = datetime.combine(d, parse_hhmm(rec["schedule"]["time"]))
    run = min(due - timedelta(hours=1), datetime.combine(d, dtime(8, 0)))
    run = max(run, datetime.combine(d, dtime(0, 0)))
    return local_to_utc(run, _tz(rec))


def plan_next(rec: dict, after: date | None = None) -> date | None:
    s = rec["schedule"]
    start = _d(rec.get("start_date")) or now_local(_tz(rec)).date()
    end = _d(rec.get("end_date"))
    if after is not None:
        frm = after + timedelta(days=1)
    else:
        now_l = now_local(_tz(rec))
        frm = now_l.date()
        if datetime.combine(frm, parse_hhmm(s["time"])) <= now_l:
            frm += timedelta(days=1)
        last = _d(rec.get("last_generated_date"))
        if last and frm <= last:
            frm = last + timedelta(days=1)
    return next_occurrence(s, frm, start, end)


def next_fields(rec: dict, d: date | None) -> dict:
    if d is None:
        return {"next_date": None, "next_run_at": None, "active": False}
    return {"next_date": d.isoformat(), "next_run_at": _run_at_utc(rec, d), "active": True}


async def create_recurring(
    uid: int, title: str, category: str, label: str, schedule: dict,
    description: str = "", ai_confidence: float | None = None, start_date: date | None = None,
) -> dict:
    now_iso = datetime.now().isoformat()
    start = start_date or now_local(schedule.get("tz") or DEFAULT_TZ).date()
    rec = {
        "uid": uid,
        "title": title,
        "description": description,
        "category": category,
        "priority": label,
        "schedule": schedule,
        "start_date": start.isoformat(),
        "end_date": None,
        "reminder_minutes": None,
        "active": True,
        "paused": False,
        "last_generated_date": None,
        "next_date": None,
        "next_run_at": None,
        "ai_detected": ai_confidence is not None,
        "ai_confidence": ai_confidence,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    rec.update(next_fields(rec, plan_next(rec)))
    res = await db_call(m.recurring_tasks_col.insert_one(rec))
    rec["_id"] = res.inserted_id
    return rec


async def get_recurring(rid: str) -> dict | None:
    oid = _oid(rid)
    if oid is None:
        return None
    return await db_call(m.recurring_tasks_col.find_one({"_id": oid}))


async def list_recurring(uid: int) -> list:
    cursor = m.recurring_tasks_col.find({"uid": uid}).sort("created_at", 1)
    return await db_call(cursor.to_list(length=None), default=[]) or []


async def update_recurring(rid: str, fields: dict):
    oid = _oid(rid)
    if oid is None:
        return
    fields = {**fields, "updated_at": datetime.now().isoformat()}
    await db_call(m.recurring_tasks_col.update_one({"_id": oid}, {"$set": fields}))


async def replan_recurring(rid: str) -> dict | None:
    rec = await get_recurring(rid)
    if not rec:
        return None
    await update_recurring(rid, next_fields(rec, plan_next(rec)))
    return await get_recurring(rid)


async def skip_next_recurring(rid: str):
    rec = await get_recurring(rid)
    if not rec or not rec.get("next_date"):
        return None
    d = _d(rec["next_date"])
    nxt = plan_next(rec, after=d)
    fields = next_fields(rec, nxt)
    fields["last_generated_date"] = d.isoformat()
    await update_recurring(rid, fields)
    return d, nxt


async def delete_recurring(rid: str):
    oid = _oid(rid)
    if oid is None:
        return
    await db_call(m.recurring_tasks_col.delete_one({"_id": oid}))
    await db_call(m.tasks_col.update_many(
        {"recurring_task_id": rid},
        {"$unset": {"recurring_task_id": "", "occurrence_date": ""}},
    ))


async def get_due_recurring(now_utc: datetime) -> list:
    cursor = m.recurring_tasks_col.find(
        {"active": True, "paused": False, "next_run_at": {"$lte": now_utc}}
    )
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def _advance(rec: dict, nxt: date | None, last: date | None = None):
    fields = next_fields(rec, nxt)
    if last is not None:
        fields["last_generated_date"] = last.isoformat()
    fields["updated_at"] = datetime.now().isoformat()
    await db_call(m.recurring_tasks_col.update_one(
        {"_id": rec["_id"], "next_run_at": rec["next_run_at"]},
        {"$set": fields},
    ))


async def generate_occurrence(rec: dict) -> dict | None:
    s = rec["schedule"]
    start = _d(rec.get("start_date"))
    end = _d(rec.get("end_date"))
    rid = str(rec["_id"])
    today = now_local(_tz(rec)).date()
    d = _d(rec.get("next_date"))

    if d is None:
        await _advance(rec, None)
        return None
    if d < today:
        await _advance(rec, next_occurrence(s, today, start, end))
        return None

    due_dt = datetime.combine(d, parse_hhmm(s["time"]))
    new_id = await next_task_id()
    task = {
        "id": new_id,
        "uid": rec["uid"],
        "text": rec["title"],
        "label": rec.get("priority") or "medium",
        "category": rec.get("category") or "other",
        "due": fmt_due(due_dt),
        "status": STATUS_PENDING,
        "pinned": False,
        "subtasks": [],
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "reminded_before": False,
        "missed_flagged": False,
        "missed_counted": False,
        "postponed_count": 0,
        "postponed_today": False,
        "source": "recurring",
        "project_id": None,
        "estimated_minutes": None,
        "recurring_task_id": rid,
        "occurrence_date": d.isoformat(),
        "reminder_minutes": rec.get("reminder_minutes"),
    }
    if rec.get("description"):
        task["description"] = rec["description"]

    created = None
    try:
        await m.tasks_col.insert_one(task)
        created = task
    except DuplicateKeyError:
        created = None

    await _advance(rec, next_occurrence(s, d + timedelta(days=1), start, end), last=d)
    return created


async def get_suggestions(uid: int) -> list:
    cursor = m.recurring_suggestions_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=None), default=[]) or []


async def add_suggestion(doc: dict) -> str:
    res = await db_call(m.recurring_suggestions_col.insert_one(doc))
    return str(res.inserted_id)


async def get_suggestion(sid: str) -> dict | None:
    oid = _oid(sid)
    if oid is None:
        return None
    return await db_call(m.recurring_suggestions_col.find_one({"_id": oid}))


async def update_suggestion(sid: str, fields: dict):
    oid = _oid(sid)
    if oid is None:
        return
    fields = {**fields, "updated_at": datetime.now().isoformat()}
    await db_call(m.recurring_suggestions_col.update_one({"_id": oid}, {"$set": fields}))