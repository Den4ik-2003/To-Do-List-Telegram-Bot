from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


async def save_draft(uid: int, shop_id: str, template_id: str | None, article_id: str | None,
                      fields: dict, rendered_text: str, photo_file_ids: list,
                      opening_sticker_file_id: str | None) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "template_id": template_id,
        "article_id": article_id,
        "fields": fields,
        "rendered_text": rendered_text,
        "photo_file_ids": photo_file_ids,
        "opening_sticker_file_id": opening_sticker_file_id,
        "status": "preview",
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_drafts_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def get_draft(draft_id: str) -> dict | None:
    return await db_call(m.shop_drafts_col.find_one({"_id": ObjectId(draft_id)}), default=None, raise_on_fail=False)


async def update_draft(draft_id: str, fields: dict):
    await db_call(m.shop_drafts_col.update_one({"_id": ObjectId(draft_id)}, {"$set": fields}))


async def delete_draft(draft_id: str):
    await db_call(m.shop_drafts_col.delete_one({"_id": ObjectId(draft_id)}))


async def add_published_post(uid: int, shop_id: str, channel_id: int, message_ids: list,
                              rendered_text: str, photo_file_ids: list,
                              template_id: str | None = None, article_id: str | None = None) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "channel_id": channel_id,
        "message_ids": message_ids,
        "rendered_text": rendered_text,
        "photo_file_ids": photo_file_ids,
        "template_id": template_id,
        "article_id": article_id,
        "published_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_published_posts_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def get_published_posts(shop_id: str, limit: int = 20) -> list:
    cursor = m.shop_published_posts_col.find({"shop_id": shop_id}).sort("published_at", -1).limit(limit)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []