from datetime import datetime, timedelta

from database import mongo as m
from database.mongo import db_call

MAX_PREVIOUS_TEXTS = 15
PREVIOUS_TEXTS_LOOKBACK_DAYS = 30


async def get_all_shops_for_broadcast() -> list:
    """Мінімальний зріз усіх магазинів (uid, назва) для щоденної розсилки
    Threads-ідей. Звертається напряму до shops_col замість
    database/shops.py, щоб не редагувати файл, якого тут немає."""
    cursor = m.shops_col.find({}, {"uid": 1, "title": 1})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_sample_product_names(shop_id: str, limit: int = 15) -> list[str]:
    """Реальні назви товарів магазину (з уже доданих артикулів) — щоб AI
    орієнтувався на СПРАВЖНЮ тематику магазину (годинники/кросівки/...),
    а не вигадував її з нуля лише з назви магазину."""
    cursor = (
        m.shop_articles_col.find({"shop_id": shop_id}, {"product_name": 1})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    return [d.get("product_name") for d in docs if d.get("product_name")]


async def add_ideas(uid: int, shop_id: str, ideas: list[dict]) -> str:
    doc = {
        "uid": uid,
        "shop_id": shop_id,
        "ideas": ideas,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(m.shop_thread_ideas_col.insert_one(doc))
    return str(result.inserted_id) if result else ""


async def get_latest(shop_id: str) -> dict | None:
    cursor = m.shop_thread_ideas_col.find({"shop_id": shop_id}).sort("created_at", -1).limit(1)
    docs = await db_call(cursor.to_list(length=1), default=[], raise_on_fail=False) or []
    return docs[0] if docs else None


async def get_recent_texts(shop_id: str, limit: int = MAX_PREVIOUS_TEXTS) -> list[str]:
    """Тексти вже надісланих ідей за останні ~30 днів — передаються в
    промпт, щоб AI не повторював і не перефразовував ті самі пости."""
    since = (datetime.now() - timedelta(days=PREVIOUS_TEXTS_LOOKBACK_DAYS)).isoformat()
    cursor = (
        m.shop_thread_ideas_col.find({"shop_id": shop_id, "created_at": {"$gte": since}})
        .sort("created_at", -1)
        .limit(20)
    )
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    texts = []
    for d in docs:
        for idea in d.get("ideas", []):
            if idea.get("text"):
                texts.append(idea["text"])
    return texts[:limit]