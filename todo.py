import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from config.settings import BOT_TOKEN, MONGO_URI, PORT
from database.mongo import init_mongo, close_mongo
from database.users import load_authorized_uids

logger = logging.getLogger("tasks_bot")


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check server started on port %s", PORT)
    return runner


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)


def register_routers(dp: Dispatcher) -> None:
    from handlers import (
        start,
        menu,
        tasks,
        ai_planner,
        ai_chat,
        ai_command,
        insights,
        goals,
        projects,
        finances,
        statistics,
        currency,
        countdown,
        weather,
        receipts,
        settings as settings_handlers,
    )

    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(ai_planner.router)
    dp.include_router(ai_chat.router)
    dp.include_router(ai_command.router)
    dp.include_router(insights.router)
    dp.include_router(goals.router)
    dp.include_router(projects.router)
    dp.include_router(statistics.router)
    dp.include_router(currency.router)
    dp.include_router(countdown.router)
    dp.include_router(weather.router)
    dp.include_router(receipts.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(menu.router)
    dp.include_router(finances.router)  # має бути справді останнім (catch-all)


async def main() -> None:
    setup_logging()
    logger.info("Starting Personal AI Planner bot...")

    await init_mongo(MONGO_URI)
    await load_authorized_uids()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher(storage=MemoryStorage())

    register_routers(dp)

    health_runner = await start_health_server()

    from scheduler.daily_jobs import register_scheduler_jobs
    register_scheduler_jobs(bot)
    logger.info("Scheduler jobs registered")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        await health_runner.cleanup()
        await close_mongo()
        await bot.session.close()
        logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user")