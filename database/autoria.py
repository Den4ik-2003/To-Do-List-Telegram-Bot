from bson import ObjectId
from datetime import datetime

from database.mongo import autoria_saved_col, db_call


async def add_saved_search(uid: int, filters: dict, label: str) -> str:
    doc = {
        "uid": uid,
        "filters": filters,
        "label": label,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(autoria_saved_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_searches(uid: int) -> list[dict]:
    cursor = autoria_saved_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def delete_saved_search(search_id, uid: int) -> bool:
    result = await db_call(
        autoria_saved_col.delete_one({"_id": ObjectId(search_id), "uid": uid})
    )
    return result.deleted_count > 0