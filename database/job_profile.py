"""
ЗМІНЕНИЙ ФАЙЛ: database/job_profile.py

Додано (для фічі "🌙 Вечірній підбір вакансій за профілем"):
- digest_seen_ids — список id вакансій, які вже були показані користувачу
  в дайджесті, зберігається прямо в документі профілю (він і так один на
  користувача). Обмежено останніми DIGEST_SEEN_LIMIT, щоб документ не ріс
  безмежно.
- digest_enabled — прапорець увімк/вимк дайджесту (за замовчуванням
  увімкнено, якщо профіль узагалі заповнено — окремого хендлера для
  вимкнення поки не робив, бо явно не просили; set_digest_enabled()
  готова, якщо захочеш додати кнопку в налаштуваннях).

Решта функцій — без змін.
"""

from datetime import datetime

from database.mongo import job_profiles_col, db_call

DIGEST_SEEN_LIMIT = 500


async def save_profile(uid: int, data: dict) -> None:
    data["uid"] = uid
    data["updated_at"] = datetime.now().isoformat()
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": data}, upsert=True)
    )


async def update_profile_field(uid: int, field: str, value: str) -> None:
    await db_call(
        job_profiles_col.update_one(
            {"uid": uid},
            {"$set": {field: value, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )
    )


async def get_profile(uid: int) -> dict | None:
    return await db_call(job_profiles_col.find_one({"uid": uid}), raise_on_fail=False)


async def has_profile(uid: int) -> bool:
    doc = await get_profile(uid)
    return bool(doc and doc.get("profession"))


# =========================================================
# 🌙 Вечірній підбір вакансій за профілем
# =========================================================

async def get_digest_seen_ids(uid: int) -> set[str]:
    doc = await get_profile(uid)
    return set((doc or {}).get("digest_seen_ids") or [])


async def add_digest_seen_ids(uid: int, new_ids: list[str]) -> None:
    if not new_ids:
        return
    existing = await get_digest_seen_ids(uid)
    existing.update(new_ids)
    # тримаємо лише останні DIGEST_SEEN_LIMIT, щоб документ не ріс безмежно
    trimmed = list(existing)[-DIGEST_SEEN_LIMIT:]
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": {"digest_seen_ids": trimmed}}, upsert=True),
        raise_on_fail=False,
    )


async def is_digest_enabled(uid: int) -> bool:
    doc = await get_profile(uid)
    # за замовчуванням увімкнено — якщо профіль заповнений, юзер явно
    # зацікавлений у підборі вакансій
    return bool((doc or {}).get("digest_enabled", True))


async def set_digest_enabled(uid: int, enabled: bool) -> None:
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": {"digest_enabled": enabled}}, upsert=True)
    )