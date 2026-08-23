from datetime import datetime

from database import mongo as m
from database.mongo import db_call


async def save_rates(rates: dict):
    doc = {**rates, "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    await db_call(m.rates_col.update_one({"_id": "latest"}, {"$set": doc}, upsert=True))


async def get_rates() -> dict | None:
    return await db_call(m.rates_col.find_one({"_id": "latest"}), raise_on_fail=False)