from datetime import datetime

from database import mongo as m
from database.mongo import db_call


async def add_example(uid: int, shop_id: str, template_id: str | None, name: str, raw_text: str, photo_file_ids: list) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "template_id": template_id,
        "name": name,
        "raw_text": raw_text,
        "photo_file_ids": photo_file_ids,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_examples_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def get_examples(shop_id: str) -> list:
    cursor = m.shop_examples_col.find({"shop_id": shop_id}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []