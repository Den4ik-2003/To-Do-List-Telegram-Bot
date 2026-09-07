"""
ЗМІНЕНИЙ ФАЙЛ: database/site_watch.py

Додано підтримку "🌐 Моніторинг сторінок" (контент-діф + AI-аналіз) поверх
ІСНУЮЧОЇ колекції site_watch_col. Старі документи (uptime-моніторинги без
поля "kind") нічим не відрізняються за поведінкою — kind трактується як
"uptime" за замовчуванням через .get("kind", "uptime") у сервісному шарі.

Жодна існуюча функція (add_site_watch, get_user_watches, get_watch,
get_all_watches, update_watch_status, save_qa_result, get_last_qa,
delete_watch) НЕ змінена.
"""

from bson import ObjectId
from datetime import datetime

from database.mongo import site_watch_col, qa_results_col, site_watch_history_col, db_call
from config.settings import PAGE_CHECK_INTERVAL_MINUTES


async def add_site_watch(uid: int, url: str, label: str = "") -> str:
    doc = {
        "uid": uid,
        "url": url,
        "label": label or url,
        "kind": "uptime",
        "last_status": None,
        "last_checked_at": None,
        "down_since": None,
        "last_qa_at": None,
        "last_qa_ok": None,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(site_watch_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_watches(uid: int) -> list[dict]:
    cursor = site_watch_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_watch(watch_id) -> dict | None:
    return await db_call(site_watch_col.find_one({"_id": ObjectId(watch_id)}))


async def get_all_watches() -> list[dict]:
    cursor = site_watch_col.find({})
    return await db_call(cursor.to_list(length=1000))


async def update_watch_status(watch_id, is_up: bool, down_since: str | None):
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {
                "last_status": is_up,
                "last_checked_at": datetime.now().isoformat(),
                "down_since": down_since,
            }},
        )
    )


async def save_qa_result(watch_id, uid: int, url: str, report: dict, is_ok: bool) -> str:
    doc = {
        "watch_id": str(watch_id),
        "uid": uid,
        "url": url,
        "report": report,
        "is_ok": is_ok,
        "checked_at": datetime.now().isoformat(),
    }
    result = await db_call(qa_results_col.insert_one(doc))
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {"last_qa_at": doc["checked_at"], "last_qa_ok": is_ok}},
        )
    )
    return str(result.inserted_id)


async def get_last_qa(watch_id) -> dict | None:
    cursor = qa_results_col.find({"watch_id": str(watch_id)}).sort("checked_at", -1).limit(1)
    results = await db_call(cursor.to_list(length=1))
    return results[0] if results else None


async def delete_watch(watch_id, uid: int) -> bool:
    result = await db_call(
        site_watch_col.delete_one({"_id": ObjectId(watch_id), "uid": uid})
    )
    return result.deleted_count > 0


# ============================================================
# НОВЕ: Моніторинг сторінок (контент-діф + AI-аналіз)
# ============================================================

async def add_page_watch(uid: int, url: str, label: str = "") -> str:
    doc = {
        "uid": uid,
        "url": url,
        "label": label or url,
        "kind": "page",
        "check_interval_minutes": PAGE_CHECK_INTERVAL_MINUTES,
        "notifications_enabled": True,
        "notify_moderate": False,
        "last_content_hash": None,
        "last_content_text": None,
        "last_content_checked_at": None,
        "last_fetch_ok": None,
        "last_change_at": None,
        "checks_count": 0,
        "changes_count": 0,
        "important_changes_count": 0,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(site_watch_col.insert_one(doc))
    return str(result.inserted_id)


async def get_watches_by_kind(kind: str) -> list[dict]:
    cursor = site_watch_col.find({"kind": kind})
    return await db_call(cursor.to_list(length=1000))


async def update_content_snapshot(watch_id, content_hash: str, content_text: str):
    """Оновлює знімок контенту + рахує перевірку. Викликається щоразу, коли
    сторінку вдалось завантажити (незалежно від того, була зміна чи ні)."""
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {
                "$set": {
                    "last_content_hash": content_hash,
                    "last_content_text": content_text,
                    "last_content_checked_at": datetime.now().isoformat(),
                    "last_fetch_ok": True,
                },
                "$inc": {"checks_count": 1},
            },
        )
    )


async def set_fetch_status(watch_id, ok: bool) -> bool:
    """Фіксує успіх/невдачу завантаження сторінки. Повертає True лише якщо
    це ПЕРЕХІД у стан "недоступна" (щоб надіслати сповіщення один раз,
    а не при кожній повторній невдалій перевірці)."""
    watch = await db_call(site_watch_col.find_one({"_id": ObjectId(watch_id)}))
    was_ok = watch.get("last_fetch_ok") if watch else None
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {"last_fetch_ok": ok, "last_content_checked_at": datetime.now().isoformat()}},
        )
    )
    return was_ok is not False and not ok


async def save_page_change(watch_id, uid: int, url: str, importance: str, change_type: str,
                            summary: str, before: str, after: str, why: str) -> str:
    checked_at = datetime.now().isoformat()
    doc = {
        "watch_id": str(watch_id),
        "uid": uid,
        "url": url,
        "importance": importance,
        "change_type": change_type,
        "summary": summary,
        "before": before,
        "after": after,
        "why_important": why,
        "checked_at": checked_at,
    }
    result = await db_call(site_watch_history_col.insert_one(doc))

    inc = {"changes_count": 1}
    if importance == "high":
        inc["important_changes_count"] = 1
    await db_call(
        site_watch_col.update_one(
            {"_id": ObjectId(watch_id)},
            {"$set": {"last_change_at": checked_at}, "$inc": inc},
        )
    )
    return str(result.inserted_id)


async def get_page_history(watch_id, limit: int = 10) -> list[dict]:
    cursor = site_watch_history_col.find({"watch_id": str(watch_id)}).sort("checked_at", -1).limit(limit)
    return await db_call(cursor.to_list(length=limit))


async def update_watch_settings(watch_id, uid: int, **fields) -> bool:
    """Універсальне оновлення полів (label, check_interval_minutes,
    notifications_enabled, notify_moderate) з перевіркою власника."""
    result = await db_call(
        site_watch_col.update_one({"_id": ObjectId(watch_id), "uid": uid}, {"$set": fields})
    )
    return result.matched_count > 0