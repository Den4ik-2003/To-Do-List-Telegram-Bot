from datetime import datetime
import logging

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from database import mongo as m
from database.mongo import DBUnavailable

logger = logging.getLogger("tasks_bot")


def normalize_article(article: str) -> str:
    """'  abc 123 ' та 'ABC123' повинні вважатись одним артикулом:
    прибираємо зайві пробіли всередині/по краях, приводимо до верхнього регістру."""
    return " ".join((article or "").strip().split()).upper()


async def find_article(shop_id: str, article: str) -> dict | None:
    norm = normalize_article(article)
    if not norm:
        return None
    try:
        return await m.shop_articles_col.find_one({"shop_id": shop_id, "article_norm": norm})
    except PyMongoError as e:
        logger.exception("find_article failed: %s", e)
        raise DBUnavailable(str(e)) from e


async def get_article(article_id: str) -> dict | None:
    try:
        return await m.shop_articles_col.find_one({"_id": ObjectId(article_id)})
    except PyMongoError as e:
        logger.exception("get_article failed: %s", e)
        raise DBUnavailable(str(e)) from e


async def add_article(uid: int, shop_id: str, article: str, product_name: str,
                       product_id: str | None = None) -> str | None:
    """Повертає inserted_id. Повертає None, якщо такий артикул вже є в цьому магазині
    (унікальність гарантує індекс у mongo.py, тож навіть паралельні запити не створять дубль)."""
    norm = normalize_article(article)
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "article": article.strip(),
        "article_norm": norm,
        "product_name": product_name,
        "product_id": product_id,
        "created_at": datetime.now().isoformat(),
    }
    try:
        result = await m.shop_articles_col.insert_one(doc)
        return str(result.inserted_id)
    except DuplicateKeyError:
        return None
    except PyMongoError as e:
        logger.exception("add_article failed: %s", e)
        raise DBUnavailable(str(e)) from e


async def get_articles(shop_id: str, limit: int = 200) -> list:
    cursor = m.shop_articles_col.find({"shop_id": shop_id}).sort("created_at", -1).limit(limit)
    try:
        return await cursor.to_list(length=None)
    except PyMongoError as e:
        logger.exception("get_articles failed: %s", e)
        raise DBUnavailable(str(e)) from e