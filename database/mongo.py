import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

logger = logging.getLogger("tasks_bot")

mongo_client: AsyncIOMotorClient | None = None
db = None
tasks_col = None
users_col = None
auth_col = None
counters_col = None
goals_col = None
projects_col = None
# Нижче — заготовки під майбутні розділи (фінанси, AI usage, AI чат),
# яких ще немає в поточному боті. Колекції не використовуються, поки
# відповідні database/*.py модулі не реалізовані.
transactions_col = None
budgets_col = None
ai_usage_col = None
ai_conversations_col = None


async def init_mongo(mongo_uri: str):
    """
    Ініціалізує з'єднання з MongoDB і повертає об'єкт db.
    Повертає db, щоб todo.py міг покласти його в dp["db"] за потреби,
    але самі модулі database/*.py як і раніше користуються глобальними
    змінними цього файлу (m.tasks_col і т.д.) — це узгоджено з тим,
    як вони вже написані.
    """
    global mongo_client, db, tasks_col, users_col, auth_col, counters_col
    global goals_col, projects_col
    global transactions_col, budgets_col, ai_usage_col, ai_conversations_col

    mongo_client = AsyncIOMotorClient(
        mongo_uri,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=15000,
        maxPoolSize=20,
        retryWrites=True,
    )
    db = mongo_client["tasks_bot"]
    tasks_col = db["tasks"]
    users_col = db["users"]
    auth_col = db["auth"]
    counters_col = db["counters"]
    goals_col = db["goals"]
    projects_col = db["projects"]

    # Колекції під фінанси, AI usage, AI чат — вже використовуються
    # відповідними database/*.py модулями (finances.py, ai_usage.py,
    # conversations.py)
    transactions_col = db["transactions"]
    budgets_col = db["budgets"]
    ai_usage_col = db["ai_usage"]
    ai_conversations_col = db["ai_conversations"]

    await ping()
    return db


async def close_mongo() -> None:
    """Акуратно закриває з'єднання з MongoDB при зупинці бота."""
    if mongo_client is not None:
        mongo_client.close()
        logger.info("MongoDB connection closed")


async def ping() -> bool:
    """Перевірка з'єднання при старті (як admin.command('ping') в оригіналі)."""
    try:
        await mongo_client.admin.command("ping")
        logger.info("MongoDB connection OK")
        return True
    except Exception:
        logger.exception("MongoDB connection FAILED at startup")
        return False


class DBUnavailable(Exception):
    pass


async def db_call(coro, default=None, retries=2, raise_on_fail=True):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await coro
        except PyMongoError as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue
    logger.exception("MongoDB error: %s", last_exc)
    if raise_on_fail:
        raise DBUnavailable(str(last_exc)) from last_exc
    return default