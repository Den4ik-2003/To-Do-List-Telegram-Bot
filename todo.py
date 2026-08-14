"""
todo.py — точка входу Personal AI Planner бота.

Файл навмисно "тонкий": він лише
    1. вантажить конфіг;
    2. підключає MongoDB;
    3. реєструє всі routers (handlers/*);
    4. запускає scheduler (ранковий план, вечірній аналіз, тижневий підсумок);
    5. стартує polling.

Вся бізнес-логіка живе в config/, database/, handlers/, services/,
keyboards/, utils/, scheduler/ — сюди нічого, крім bootstrap-коду,
додавати не потрібно.

ПРИМІТКА: цей файл написаний під структуру проекту з ТЗ і посилається
на модулі (config.settings, database.mongo, handlers.*, scheduler.daily_jobs),
які ще потрібно перенести/створити з твого існуючого коду. Це наступний
крок — див. README.md, розділ "Що мені потрібно від тебе".
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import settings
from database.mongo import init_mongo, close_mongo

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
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

    # Порядок важливий: специфічні FSM-роутери — перед загальним menu-роутером,
    # інакше menu.py може "перехопити" повідомлення, призначені для FSM-стейтів.
    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(ai_planner.router)
    dp.include_router(ai_chat.router)
    dp.include_router(goals.router)
    dp.include_router(projects.router)
    dp.include_router(finances.router)
    dp.include_router(statistics.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(menu.router)  # menu — останній, як "fallback" на головне меню


async def main() -> None:
    setup_logging()
    logger.info("Starting Personal AI Planner bot...")

    # 1. Mongo
    db = await init_mongo(settings.MONGODB_URI)
    logger.info("MongoDB connected")

    # 2. Bot & Dispatcher
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db  # доступний у будь-якому handler як db: AsyncIOMotorDatabase

    register_routers(dp)

    # 3. Scheduler (ранковий план / вечірній аналіз / тижневий підсумок)
    # ВАЖЛИВО: scheduler НЕ повинен створювати другий polling-інстанс,
    # інакше отримаємо TelegramConflictError (див. п.34 ТЗ).
    from scheduler.daily_jobs import setup_scheduler

    scheduler = setup_scheduler(bot=bot, db=db)
    scheduler.start()
    logger.info("Scheduler started")

    # 4. Health endpoint для Render (якщо вже був — не чіпаємо; якщо ні,
    # додається окремо в services/notification_service.py або окремому
    # aiohttp web-app за потреби — залежить від того, що вже є у твоєму коді)

    try:
        # На випадок якщо раніше залишився webhook — скидаємо його,
        # інакше polling впаде з TelegramConflictError
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await close_mongo()
        await bot.session.close()
        logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user")