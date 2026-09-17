"""
ЗМІНЕНИЙ ФАЙЛ: database/jobs.py

Додано відносно попередньої версії (для фічі "🌙 Автопошук вакансій"):
- add_search_watch(): новий опціональний параметр title — людська назва
  автопошуку (напр. "Frontend Developer", "Робота в Польщі"). Старі
  виклики add_search_watch(uid, criteria, seen_ids) без title і далі
  працюють ІДЕНТИЧНО — title просто буде None, і хендлер показує
  criteria.profession як запасний варіант назви.
- get_watch(): отримати один автопошук за id (опціонально з перевіркою
  власника).
- append_pending_digest() / get_and_clear_pending_digest(): накопичення
  знайдених за день вакансій прямо в документі автопошуку — обідній
  прогін лише додає туди знахідки, вечірній дістає й ОЧИЩАЄ (атомарно),
  щоб сформувати єдиний вечірній підсумок без повторів.

Решта функцій — без змін.
"""

from datetime import datetime
from collections import Counter

from bson import ObjectId
from pymongo import ReturnDocument

from database.mongo import job_saved_col, job_searches_col, job_feedback_col, db_call


async def save_vacancy(uid: int, vacancy: dict, status: str = "saved") -> str:
    doc = {
        "uid": uid,
        "title": vacancy.get("title"),
        "company": vacancy.get("company"),
        "location": vacancy.get("location"),
        "work_format": vacancy.get("work_format"),
        "salary": vacancy.get("salary"),
        "url": vacancy.get("url"),
        "source": vacancy.get("source"),
        "sources": vacancy.get("sources") or [vacancy.get("source")],
        "match_percent": vacancy.get("match_percent"),
        "cover_letter": vacancy.get("cover_letter"),
        "status": status,
        "saved_at": datetime.now().isoformat(),
    }
    result = await db_call(job_saved_col.insert_one(doc))
    return str(result.inserted_id)


async def update_cover_letter(saved_id, cover_letter: str):
    await db_call(
        job_saved_col.update_one(
            {"_id": ObjectId(saved_id)}, {"$set": {"cover_letter": cover_letter}}
        )
    )


async def update_status(saved_id, uid: int, status: str) -> bool:
    result = await db_call(
        job_saved_col.update_one(
            {"_id": ObjectId(saved_id), "uid": uid}, {"$set": {"status": status}}
        )
    )
    return result.modified_count > 0


async def get_saved(uid: int) -> list[dict]:
    cursor = job_saved_col.find({"uid": uid}).sort("saved_at", -1)
    return await db_call(cursor.to_list(length=100))


async def is_saved(uid: int, url: str) -> bool:
    doc = await db_call(job_saved_col.find_one({"uid": uid, "url": url}), raise_on_fail=False)
    return bool(doc)


async def delete_saved(saved_id, uid: int) -> bool:
    result = await db_call(
        job_saved_col.delete_one({"_id": ObjectId(saved_id), "uid": uid})
    )
    return result.deleted_count > 0


async def add_search_watch(uid: int, criteria: dict, seen_ids: list[str], title: str | None = None) -> str:
    doc = {
        "uid": uid,
        "title": title,
        "criteria": criteria,
        "seen_ids": seen_ids,
        "pending_digest": [],
        "active": True,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(job_searches_col.insert_one(doc))
    return str(result.inserted_id)


async def get_watch(watch_id, uid: int | None = None) -> dict | None:
    query = {"_id": ObjectId(watch_id)}
    if uid is not None:
        query["uid"] = uid
    return await db_call(job_searches_col.find_one(query), raise_on_fail=False)


async def get_user_watches(uid: int) -> list[dict]:
    cursor = job_searches_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=50))


async def get_all_watches() -> list[dict]:
    cursor = job_searches_col.find({"active": {"$ne": False}})
    return await db_call(cursor.to_list(length=500))


async def update_watch_seen(watch_id, seen_ids: list[str]):
    await db_call(
        job_searches_col.update_one(
            {"_id": ObjectId(watch_id)}, {"$set": {"seen_ids": seen_ids}}
        )
    )


async def set_watch_active(watch_id, uid: int, active: bool) -> bool:
    result = await db_call(
        job_searches_col.update_one(
            {"_id": ObjectId(watch_id), "uid": uid}, {"$set": {"active": active}}
        )
    )
    return result.modified_count > 0


async def delete_watch(watch_id, uid: int) -> bool:
    result = await db_call(
        job_searches_col.delete_one({"_id": ObjectId(watch_id), "uid": uid})
    )
    return result.deleted_count > 0


async def add_feedback(uid: int, vacancy: dict, reason: str):
    doc = {
        "uid": uid,
        "title": vacancy.get("title"),
        "company": vacancy.get("company"),
        "reason": reason,
        "created_at": datetime.now().isoformat(),
    }
    await db_call(job_feedback_col.insert_one(doc))


async def get_recent_feedback(uid: int, limit: int = 10) -> list[dict]:
    cursor = job_feedback_col.find({"uid": uid}).sort("created_at", -1).limit(limit)
    return await db_call(cursor.to_list(length=limit))


async def get_stats(uid: int) -> dict:
    saved = await get_saved(uid)
    status_counts = Counter(v.get("status", "saved") for v in saved)
    matches = [v["match_percent"] for v in saved if v.get("match_percent") is not None]
    companies = Counter(v.get("company") for v in saved if v.get("company"))
    professions = Counter(v.get("title") for v in saved if v.get("title"))
    return {
        "total_saved": len(saved),
        "applied": status_counts.get("applied", 0),
        "response": status_counts.get("response", 0),
        "interview": status_counts.get("interview", 0),
        "hired": status_counts.get("hired", 0),
        "rejected": status_counts.get("rejected", 0),
        "avg_match": round(sum(matches) / len(matches), 1) if matches else None,
        "top_companies": companies.most_common(3),
        "top_titles": professions.most_common(3),
    }


async def mark_applied(uid: int, vacancy: dict) -> None:
    """Позначає вакансію як 'applied'. Якщо вона вже збережена (⭐) —
    оновлює її статус; якщо ні — зберігає одразу зі статусом 'applied'.
    Викликається ТІЛЬКИ після фактичної подачі заявки (автоматичної або
    підтвердженої користувачем вручну) — ніколи заздалегідь."""
    existing = await db_call(
        job_saved_col.find_one({"uid": uid, "url": vacancy.get("url")}), raise_on_fail=False
    )
    if existing:
        await update_status(str(existing["_id"]), uid, "applied")
    else:
        await save_vacancy(uid, vacancy, status="applied")


# =========================================================
# НОВЕ: 🌙 Автопошук — накопичення знахідок для вечірнього дайджесту
# =========================================================

_PENDING_DIGEST_CAP = 20
_PENDING_DIGEST_FIELDS = (
    "id", "url", "title", "company", "location", "work_format",
    "salary", "experience", "requirements", "source", "sources",
)


def _strip_vacancy_for_storage(v: dict) -> dict:
    """Зберігаємо в pending_digest лише поля, потрібні для показу картки
    ввечері (без _score — його рахуємо заново під час формування
    дайджесту, бо профіль користувача міг змінитись між обідом і вечором)."""
    return {k: v.get(k) for k in _PENDING_DIGEST_FIELDS}


async def append_pending_digest(watch_id, vacancies: list[dict]) -> None:
    """Додає нові вакансії в pending_digest автопошуку, дедуплікуючи по
    url із тим, що вже там накопичено (напр. з обіднього прогону)."""
    if not vacancies:
        return
    doc = await db_call(job_searches_col.find_one({"_id": ObjectId(watch_id)}), raise_on_fail=False)
    existing = (doc or {}).get("pending_digest") or []
    seen_urls = {v.get("url") for v in existing}
    for v in vacancies:
        if v.get("url") not in seen_urls:
            existing.append(_strip_vacancy_for_storage(v))
            seen_urls.add(v.get("url"))
    existing = existing[-_PENDING_DIGEST_CAP:]
    await db_call(
        job_searches_col.update_one({"_id": ObjectId(watch_id)}, {"$set": {"pending_digest": existing}}),
        raise_on_fail=False,
    )


async def get_and_clear_pending_digest(watch_id) -> list[dict]:
    """Атомарно дістає накопичені за день вакансії й одразу очищає поле —
    щоб наступний день не показав їх повторно у вечірньому дайджесті."""
    doc = await db_call(
        job_searches_col.find_one_and_update(
            {"_id": ObjectId(watch_id)},
            {"$set": {"pending_digest": []}},
            return_document=ReturnDocument.BEFORE,
        ),
        raise_on_fail=False,
    )
    return (doc or {}).get("pending_digest") or []