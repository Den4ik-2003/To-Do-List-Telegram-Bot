from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


async def get_stickers(shop_id: str) -> list:
    cursor = m.shop_stickers_col.find({"shop_id": shop_id}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def add_sticker(uid: int, shop_id: str, alias: str, file_id: str) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "alias": alias,
        "file_id": file_id,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_stickers_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def delete_sticker(sticker_id: str):
    await db_call(m.shop_stickers_col.delete_one({"_id": ObjectId(sticker_id)}))