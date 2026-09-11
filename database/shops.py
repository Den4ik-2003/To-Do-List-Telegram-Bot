from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


async def get_shops(uid: int) -> list:
    cursor = m.shops_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_shop(shop_id: str) -> dict | None:
    return await db_call(m.shops_col.find_one({"_id": ObjectId(shop_id)}), default=None, raise_on_fail=False)


async def add_shop(uid: int, title: str) -> str:
    doc = {
        "uid": uid,
        "title": title,
        "channel_id": None,
        "channel_title": None,
        "channel_username": None,
        "last_template_id": None,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shops_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def rename_shop(shop_id: str, title: str):
    await db_call(m.shops_col.update_one({"_id": ObjectId(shop_id)}, {"$set": {"title": title}}))


async def set_channel(shop_id: str, channel_id: int, channel_title: str, channel_username: str | None):
    await db_call(m.shops_col.update_one(
        {"_id": ObjectId(shop_id)},
        {"$set": {"channel_id": channel_id, "channel_title": channel_title, "channel_username": channel_username}},
    ))


async def unset_channel(shop_id: str):
    await db_call(m.shops_col.update_one(
        {"_id": ObjectId(shop_id)},
        {"$set": {"channel_id": None, "channel_title": None, "channel_username": None}},
    ))


async def set_last_template(shop_id: str, template_id: str | None):
    await db_call(m.shops_col.update_one({"_id": ObjectId(shop_id)}, {"$set": {"last_template_id": template_id}}))


async def delete_shop(shop_id: str):
    oid = ObjectId(shop_id)
    await db_call(m.shop_templates_col.delete_many({"shop_id": shop_id}))
    await db_call(m.shop_examples_col.delete_many({"shop_id": shop_id}))
    await db_call(m.shop_stickers_col.delete_many({"shop_id": shop_id}))
    await db_call(m.shop_drafts_col.delete_many({"shop_id": shop_id}))
    await db_call(m.shops_col.delete_one({"_id": oid}))