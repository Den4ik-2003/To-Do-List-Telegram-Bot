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
rates_col = None
events_col = None
transactions_col = None
budgets_col = None
ai_usage_col = None
ai_conversations_col = None
olx_tracked_col = None
resale_saved_col = None
business_ideas_col = None
site_watch_col = None
qa_results_col = None
job_profiles_col = None
job_searches_col = None
job_saved_col = None
creative_generations_col = None
autoria_saved_col = None


async def init_mongo(mongo_uri: str):
    global mongo_client, db, tasks_col, users_col, auth_col, counters_col
    global goals_col, projects_col, rates_col, events_col
    global transactions_col, budgets_col, ai_usage_col, ai_conversations_col
    global olx_tracked_col, resale_saved_col, business_ideas_col, site_watch_col
    global qa_results_col
    global job_profiles_col, job_searches_col, job_saved_col
    global creative_generations_col
    global autoria_saved_col

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
    rates_col = db["rates"]
    events_col = db["events"]

    transactions_col = db["transactions"]
    budgets_col = db["budgets"]
    ai_usage_col = db["ai_usage"]
    ai_conversations_col = db["ai_conversations"]
    olx_tracked_col = db["olx_tracked"]
    resale_saved_col = db["resale_saved"]
    business_ideas_col = db["business_ideas"]
    site_watch_col = db["site_watch"]
    qa_results_col = db["qa_results"]

    job_profiles_col = db["job_profiles"]
    job_searches_col = db["job_searches"]
    job_saved_col = db["job_saved"]

    creative_generations_col = db["creative_generations"]

    autoria_saved_col = db["autoria_saved"]

    await ping()
    return db


async def close_mongo() -> None:
    if mongo_client is not None:
        mongo_client.close()
        logger.info("MongoDB connection closed")


async def ping() -> bool:
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