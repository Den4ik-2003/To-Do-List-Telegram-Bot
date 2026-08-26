from datetime import datetime

from bson import ObjectId

from database.mongo import business_ideas_col, db_call


async def save_idea(uid: int, idea_text: str, plan: dict) -> str:
    doc = {
        "uid": uid,
        "idea_text": idea_text,
        "plan": plan,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(business_ideas_col.insert_one(doc))
    return str(result.inserted_id)


async def get_user_ideas(uid: int) -> list[dict]:
    cursor = business_ideas_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=50))


async def get_idea(idea_id, uid: int) -> dict | None:
    return await db_call(
        business_ideas_col.find_one({"_id": ObjectId(idea_id), "uid": uid})
    )


async def delete_idea(idea_id, uid: int) -> bool:
    result = await db_call(
        business_ideas_col.delete_one({"_id": ObjectId(idea_id), "uid": uid})
    )
    return result.deleted_count > 0