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
resale_monitors_col = None
business_ideas_col = None
site_watch_col = None
site_watch_history_col = None
qa_results_col = None
job_profiles_col = None
job_searches_col = None
job_saved_col = None
job_feedback_col = None
creative_generations_col = None
autoria_saved_col = None
worktime_col = None

olx_deals_col = None
olx_user_settings_col = None
olx_search_stats_col = None

favorite_recipes_col = None
recipe_history_col = None
shopping_items_col = None
cooking_sessions_col = None

shops_col = None
shop_templates_col = None
shop_examples_col = None
shop_stickers_col = None
shop_drafts_col = None
shop_published_posts_col = None
shop_articles_col = None


async def init_mongo(mongo_uri: str):
    global mongo_client, db, tasks_col, users_col, auth_col, counters_col
    global goals_col, projects_col, rates_col, events_col
    global transactions_col, budgets_col, ai_usage_col, ai_conversations_col
    global olx_tracked_col, resale_saved_col, resale_monitors_col, business_ideas_col, site_watch_col
    global site_watch_history_col
    global qa_results_col
    global job_profiles_col, job_searches_col, job_saved_col, job_feedback_col
    global creative_generations_col
    global autoria_saved_col
    global olx_deals_col, olx_user_settings_col, olx_search_stats_col
    global favorite_recipes_col, recipe_history_col, shopping_items_col, cooking_sessions_col
    global worktime_col
    global shops_col, shop_templates_col, shop_examples_col, shop_stickers_col
    global shop_drafts_col, shop_published_posts_col, shop_articles_col

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
    resale_monitors_col = db["resale_monitors"]
    business_ideas_col = db["business_ideas"]
    site_watch_col = db["site_watch"]
    site_watch_history_col = db["site_watch_history"]
    qa_results_col = db["qa_results"]

    job_profiles_col = db["job_profiles"]
    job_searches_col = db["job_searches"]
    job_saved_col = db["job_saved"]
    job_feedback_col = db["job_feedback"]

    creative_generations_col = db["creative_generations"]

    autoria_saved_col = db["autoria_saved"]

    olx_deals_col = db["olx_deals"]
    olx_user_settings_col = db["olx_user_settings"]
    olx_search_stats_col = db["olx_search_stats"]

    favorite_recipes_col = db["favorite_recipes"]
    recipe_history_col = db["recipe_history"]
    shopping_items_col = db["shopping_items"]
    cooking_sessions_col = db["active_cooking_sessions"]

    worktime_col = db["worktime_entries"]

    shops_col = db["shops"]
    shop_templates_col = db["shop_templates"]
    shop_examples_col = db["shop_examples"]
    shop_stickers_col = db["shop_stickers"]
    shop_drafts_col = db["shop_drafts"]
    shop_published_posts_col = db["shop_published_posts"]
    shop_articles_col = db["shop_articles"]

    await ping()
    await _ensure_shop_indexes()
    return db


async def _ensure_shop_indexes():
    """Індекси, специфічні для магазинів/артикулів. Викликається один раз при старті."""
    try:
        await shop_articles_col.create_index(
            [("shop_id", 1), ("article_norm", 1)],
            unique=True,
            name="uniq_shop_article",
        )
        await shop_templates_col.create_index([("shop_id", 1)], name="shop_id_idx")
        await shop_examples_col.create_index([("shop_id", 1)], name="shop_id_idx")
    except Exception:
        logger.exception("Failed to ensure shop indexes")


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
    try:
        return await coro
    except PyMongoError as e:
        logger.exception("MongoDB error: %s", e)
        if raise_on_fail:
            raise DBUnavailable(str(e)) from e
        return default