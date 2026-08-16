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
    """
    Легкий health-check endpoint для cron-job.org / Render keep-alive пінгів.
    Повертає мінімальний текст, щоб не впиратись у ліміт розміру відповіді
    зовнішніх cron-сервісів ("output too large").
    """
    return web.Response(text="OK")


async def start_health_server() -> web.AppRunner:
    """
    Мінімальний HTTP-сервер лише для того, щоб Render (Web Service)
    бачив відкритий порт і не вважав контейнер "нездоровим".
    Бот працює через long polling, а не через цей сервер — тут просто
    health-check endpoint(и).
    """
    app = web.Application()
    # Кореневий маршрут — для Render'a, щоб бачив, що сервіс живий.
    app.router.add_get("/", health_check)
    # Окремий /health — саме його треба вказувати в cron-job.org,
    # щоб уникнути помилки "Failed (output too large)".
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

    # Порядок важливий: специфічні FSM-роутери і роутери з ТОЧНИМИ
    # текстовими кнопками — перед будь-якими "catch-all" роутерами.
    #
    # УВАГА: finances.router містить quick_add_catch_all — хендлер
    # @router.message(StateFilter(None), F.text), який збігається з
    # БУДЬ-ЯКИМ текстовим повідомленням (спроба розпізнати "швидку
    # транзакцію"). Якщо цей роутер зареєструвати РАНІШЕ за menu.router,
    # він перехопить на себе кнопки на кшталт "◀️ Головне меню" та
    # "💡 Поради" ще до того, як їх побачить menu.router — бот мовчатиме,
    # ніби кнопка не працює. Тому finances.router має йти САМИМ ОСТАННІМ.
    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(ai_planner.router)
    dp.include_router(ai_chat.router)
    dp.include_router(goals.router)
    dp.include_router(projects.router)
    dp.include_router(statistics.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(menu.router)
    dp.include_router(finances.router)  # має бути справді останнім (catch-all)


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

    # 3. Health-check сервер для Render (Web Service вимагає відкритий порт)
    health_runner = await start_health_server()

    # 4. Scheduler (нагадування / опівнічний rollover / вечірній звіт /
    # ранковий AI-план — усі запускаються як background asyncio-таски,
    # НЕ як другий polling-інстанс, інакше отримаємо TelegramConflictError)
    from scheduler.daily_jobs import register_scheduler_jobs

    register_scheduler_jobs(bot)
    logger.info("Scheduler jobs registered")

    try:
        # На випадок якщо раніше залишився webhook — скидаємо його,
        # інакше polling впаде з TelegramConflictError
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