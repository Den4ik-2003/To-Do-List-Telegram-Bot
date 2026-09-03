"""
ЗМІНЕНИЙ ФАЙЛ: твій головний файл запуску бота (той, що містить main(),
register_routers(), Bot(...), dp.start_polling(...)).

ВИПРАВЛЕНО (фікс бага "не генерує рецепт" / "AI-планувальник тимчасово
недоступний" при спробі отримати рецепт): у aiogram повідомлення проходить
через роутери СУВОРО в порядку їх реєстрації, і як тільки якийсь хендлер
підходить під подію — обробка зупиняється, до наступних роутерів подія вже
не доходить. kitchen.router реєструвався ОСТАННІМ (після ai_planner.router
і ai_chat.router), тому текстове повідомлення користувача (наприклад
"Сирники"), надіслане у відповідь на запит kitchen.py в стані
KitchenStates.waiting_dish, могло перехоплюватись більш широким текстовим
хендлером у ai_planner.py чи ai_chat.py раніше, ніж доходило до kitchen.py
— звідси й повідомлення "AI-планувальник тимчасово недоступний" (текст,
який видає саме ai_planner.py, а не kitchen.py).

Фікс: kitchen.router тепер реєструється РАНІШЕ за ai_planner.router і
ai_chat.router — одразу після tasks.router. Це не єдиний можливий фікс
(альтернатива — звузити фільтр хендлера в ai_planner.py до конкретного
стану FSM), але це найбезпечніша зміна, яка нічого не ламає в інших
роутерах і гарантовано віддає пріоритет kitchen.router.

Все інше в файлі — 1:1 як було.
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def register_routers(dp: Dispatcher) -> None:
    from handlers import (
        start,
        menu,
        tasks,
        kitchen,       # ЗМІНЕНО: 🍳 Кухня піднята вище — реєструється до
                       # ai_planner/ai_chat, щоб ті не перехоплювали
                       # текстові повідомлення, призначені для kitchen.py
                       # (напр. назву страви в стані waiting_dish)
        ai_planner,
        ai_chat,
        voice,
        translator,
        nearby,
        decision,
        product_photo,
        resale,
        business,
        insights,
        goals,
        projects,
        finances,
        statistics,
        currency,
        countdown,
        weather,
        receipts,
        olx,
        autoria,
        movie,
        site_watch,
        job_profile,
        jobs,
        settings as settings_handlers,
    )

    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(kitchen.router)  # ЗМІНЕНО: реєструється до ai_planner/ai_chat
    dp.include_router(ai_planner.router)
    dp.include_router(ai_chat.router)
    dp.include_router(voice.router)
    dp.include_router(translator.router)
    dp.include_router(nearby.router)
    dp.include_router(decision.router)
    dp.include_router(product_photo.router)
    dp.include_router(resale.router)
    dp.include_router(business.router)
    dp.include_router(insights.router)
    dp.include_router(goals.router)
    dp.include_router(projects.router)
    dp.include_router(statistics.router)
    dp.include_router(currency.router)
    dp.include_router(countdown.router)
    dp.include_router(weather.router)
    dp.include_router(receipts.router)
    dp.include_router(olx.router)
    dp.include_router(autoria.router)
    dp.include_router(movie.router)
    dp.include_router(site_watch.router)
    dp.include_router(job_profile.router)
    dp.include_router(jobs.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(menu.router)
    dp.include_router(finances.router)


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

    from scheduler.olx_jobs import register_olx_jobs
    from scheduler.resale_jobs import register_resale_jobs
    from scheduler.site_watch_jobs import register_site_watch_jobs
    from scheduler.jobs_watch_jobs import register_jobs_watch_jobs

    olx_scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    register_olx_jobs(olx_scheduler, bot)
    register_resale_jobs(olx_scheduler, bot)
    register_site_watch_jobs(olx_scheduler, bot)
    register_jobs_watch_jobs(olx_scheduler, bot)
    olx_scheduler.start()
    logger.info("OLX/resale/site-watch/jobs scheduler started")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        olx_scheduler.shutdown(wait=False)
        await health_runner.cleanup()
        await close_mongo()
        await bot.session.close()
        logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user")