from datetime import date

from database import mongo as m
from database.mongo import db_call


async def add_event(uid: int, name: str, day: int, month: int | None = None,
                     year: int | None = None, recurring: str = "once"):
    doc = {
        "uid": uid,
        "name": name,
        "day": day,
        "month": month,
        "year": year,
        "recurring": recurring,  # "once" | "monthly" | "yearly"
    }
    await db_call(m.events_col.update_one(
        {"uid": uid, "name": name},
        {"$set": doc},
        upsert=True,
    ))


async def get_user_events(uid: int) -> list:
    cursor = m.events_col.find({"uid": uid}, {"_id": 0})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def delete_event(uid: int, name: str):
    await db_call(m.events_col.delete_one({"uid": uid, "name": name}))


def next_event_date(ev: dict) -> date | None:
    today = date.today()
    recurring = ev.get("recurring", "once")
    day = ev.get("day")
    if not day:
        return None

    if recurring == "monthly":
        month, year = today.month, today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate < today:
            month += 1
            if month > 12:
                month, year = 1, year + 1
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
        return candidate

    month = ev.get("month")
    if not month:
        return None

    if recurring == "yearly":
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate < today:
            candidate = date(year + 1, month, day)
        return candidate

    # "once"
    year = ev.get("year") or today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate < today and not ev.get("year"):
        candidate = date(year + 1, month, day)
    return candidate