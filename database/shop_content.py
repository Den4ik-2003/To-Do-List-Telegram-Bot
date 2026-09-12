from datetime import datetime

from bson import ObjectId

from database import mongo as m
from database.mongo import db_call


# ============================================================
# Templates
# ============================================================

async def get_templates(shop_id: str) -> list:
    cursor = m.shop_templates_col.find({"shop_id": shop_id}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_template(template_id: str) -> dict | None:
    return await db_call(m.shop_templates_col.find_one({"_id": ObjectId(template_id)}), default=None, raise_on_fail=False)


async def add_template(uid: int, shop_id: str, name: str, text_template: str, placeholders: list,
                        opening_sticker_file_id: str | None) -> str:
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


def migrate_template(t: dict) -> dict:
    """Адаптер для шаблонів, створених до появи placeholders/opening_sticker_file_id.
    Нічого не видаляє з БД — тільки доповнює об'єкт у пам'яті значеннями за замовчуванням,
    щоб старі шаблони не падали в новому коді."""
    t.setdefault("placeholders", [])
    t.setdefault("opening_sticker_file_id", None)
    t.setdefault("name", t.get("title", "Без назви"))
    t.setdefault("text_template", t.get("text", ""))
    return t


# ============================================================
# Examples (сирий текст, з якого AI/вручну зроблено шаблон)
# ============================================================

async def add_example(uid: int, shop_id: str, template_id: str | None, name: str, raw_text: str,
                       photo_file_ids: list) -> str:
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


# ============================================================
# Stickers
# ============================================================

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