from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


async def get_templates(shop_id: str) -> list:
    cursor = m.shop_templates_col.find({"shop_id": shop_id}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_template(template_id: str) -> dict | None:
    return await db_call(m.shop_templates_col.find_one({"_id": ObjectId(template_id)}), default=None, raise_on_fail=False)


async def add_template(uid: int, shop_id: str, name: str, text_template: str, placeholders: list, opening_sticker_file_id: str | None) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "name": name,
        "text_template": text_template,
        "placeholders": placeholders,
        "opening_sticker_file_id": opening_sticker_file_id,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_templates_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def update_template(template_id: str, fields: dict):
    await db_call(m.shop_templates_col.update_one({"_id": ObjectId(template_id)}, {"$set": fields}))


async def delete_template(template_id: str):
    await db_call(m.shop_templates_col.delete_one({"_id": ObjectId(template_id)}))