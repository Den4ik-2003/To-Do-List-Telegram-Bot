import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

logger = logging.getLogger("tasks_bot")


class DBUnavailable(Exception):
    pass


mongo_client: AsyncIOMotorClient | None = None
db = None

users_col = None
auth_col = None
counters_col = None
tasks_col = None
projects_col = None
goals_col = None
transactions_col = None
budgets_col = None
ai_usage_col = None
ai_conversations_col = None
notifications_col = None


def init_mongo(mongo_uri: str) -> AsyncIOMotorClient:
    global mongo_client, db
    global users_col, auth_col, counters_col, tasks_col, projects_col, goals_col
    global transactions_col, budgets_col, ai_usage_col, ai_conversations_col, notifications_col

    mongo_client = AsyncIOMotorClient(
        mongo_uri,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=15000,
        maxPoolSize=20,
        retryWrites=True,
    )
    db = mongo_client["tasks_bot"]

    users_col = db["users"]
    auth_col = db["auth"]
    counters_col = db["counters"]
    tasks_col = db["tasks"]
    projects_col = db["projects"]
    goals_col = db["goals"]
    transactions_col = db["transactions"]
    budgets_col = db["budgets"]
    ai_usage_col = db["ai_usage"]
    ai_conversations_col = db["ai_conversations"]
    notifications_col = db["notifications"]

    return mongo_client


async def ping():
    await mongo_client.admin.command("ping")


async def ensure_indexes():
    await tasks_col.create_index([("uid", 1), ("status", 1)])
    await tasks_col.create_index([("uid", 1), ("due", 1)])
    await tasks_col.create_index("id", unique=True)
    await tasks_col.create_index([("uid", 1), ("project_id", 1)])

    await projects_col.create_index([("uid", 1), ("status", 1)])
    await projects_col.create_index("created_at")

    await goals_col.create_index([("uid", 1), ("status", 1)])
    await goals_col.create_index("created_at")

    await transactions_col.create_index([("uid", 1), ("date", -1)])
    await transactions_col.create_index([("uid", 1), ("project_id", 1)])
    await transactions_col.create_index([("uid", 1), ("type", 1)])

    await budgets_col.create_index([("uid", 1), ("category", 1)])

    await ai_usage_col.create_index([("uid", 1), ("date", 1)], unique=True)
    await ai_conversations_col.create_index("uid", unique=True)
    await notifications_col.create_index([("uid", 1), ("created_at", -1)])


async def db_call(coro, default=None, retries: int = 2, raise_on_fail: bool = True):
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