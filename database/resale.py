from datetime import datetime

from bson import ObjectId

from database.mongo import resale_saved_col, db_call


async def save_opportunity(uid: int, item: dict) -> str:
    doc = {
        "uid": uid,
        "url": item.get("url"),
        "title": item.get("title"),
        "purchase_price": item.get("purchase_price"),
        "market_price": item.get("market_price"),
        "resale_price": item.get("resale_price"),
        "profit": item.get("profit"),
        "margin": item.get("margin"),
        "currency": item.get("currency", "UAH"),
        "source": item.get("source", "olx.ua"),
        "saved_at": datetime.now().isoformat(),
    }
    result = await db_call(resale_saved_col.insert_one(doc))
    return str(result.inserted_id)


async def get_saved(uid: int) -> list[dict]:
    cursor = resale_saved_col.find({"uid": uid})
    return await db_call(cursor.to_list(length=100))


async def get_all_saved() -> list[dict]:
    cursor = resale_saved_col.find({})
    return await db_call(cursor.to_list(length=1000))


async def update_saved_price(item_id, new_price: float):
    await db_call(
        resale_saved_col.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"purchase_price": new_price}},
        )
    )


async def delete_saved(item_id, uid: int) -> bool:
    result = await db_call(
        resale_saved_col.delete_one({"_id": ObjectId(item_id), "uid": uid})
    )
    return result.deleted_count > 0