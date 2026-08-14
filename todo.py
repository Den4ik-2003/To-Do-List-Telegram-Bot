import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import BOT_TOKEN, MONGO_URI
from database.mongo import init_mongo, close_mongo
from database.users import load_authorized_uids

logger = logging.getLogger("tasks_bot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Шумні бібліотеки — тихіше, щоб не забивати Render logs
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)


def register_routers(dp: Dispatcher) -> None:
    """
    Реєструє всі handlers. Імпорти навмисно тут, а не на верхньому рівні
    файлу, щоб уникнути циклічних імпортів між handlers <-> keyboards.
    """
    from handlers import (
        start,
        menu,
        tasks,
        ai_planner,
        ai_chat,
        goals,
        projects,
        finances,
        statistics,
        settings as settings_handlers,
    )

    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(ai_planner.router)
    dp.include_router(ai_chat.router)
    dp.include_router(goals.router)
    dp.include_router(projects.router)
    dp.include_router(finances.router)
    dp.include_router(statistics.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(menu.router)  
    
async def main() -> None:
    setup_logging()
    logger.info("Starting Personal AI Planner bot...")

    # 1. Mongo
    await init_mongo(MONGO_URI)
    await load_authorized_uids()

    # 2. Bot & Dispatcher
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher(storage=MemoryStorage())

    register_routers(dp)

    from scheduler.daily_jobs import register_scheduler_jobs

    register_scheduler_jobs(bot)
    logger.info("Scheduler jobs registered")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        await close_mongo()
        await bot.session.close()
        logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user")