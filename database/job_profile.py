from datetime import datetime

from database.mongo import job_profiles_col, db_call


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