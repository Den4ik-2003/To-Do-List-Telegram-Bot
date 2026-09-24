import asyncio
import logging
import sys

from database import orders as orders_db
from database import websites as websites_db
from services import order_notify_service

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


async def handle_order(request: web.Request) -> web.Response:
    site_id = request.match_info.get("site_id", "")
    if not site_id:
        return web.json_response({"ok": False, "error": "missing site_id"}, status=400)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    headers = {"Access-Control-Allow-Origin": "*"}

    website = await websites_db.get_website_by_id_any_owner(site_id)
    if not website:
        return web.json_response({"ok": False, "error": "site not found"}, status=404, headers=headers)

    name = str(payload.get("name", "")).strip()[:200]
    phone = str(payload.get("phone", "")).strip()[:50]
    if not name or not phone:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400, headers=headers)

    product = str(payload.get("product", ""))[:300]
    comment = str(payload.get("comment", ""))[:1000]

    order_id = await orders_db.create_order(
        site_id=site_id,
        owner_uid=website["uid"],
        site_name=website.get("siteName", ""),
        name=name,
        phone=phone,
        product=product,
        comment=comment,
    )

    bot = request.app["bot"]
    config = await websites_db.get_notification_config(site_id)
    order = {"name": name, "phone": phone, "product": product, "comment": comment}

    delivered, error = await order_notify_service.deliver_order_notification(config, order, bot)
    if delivered:
        await orders_db.mark_delivered(order_id)
    else:
        await orders_db.mark_delivery_failed(order_id, error or "unknown_error")
        logger.error("Не вдалося доставити замовлення %s в Telegram: %s", order_id, error)

    return web.json_response({"ok": True, "order_id": order_id, "delivered": delivered}, headers=headers)


async def handle_order_options(request: web.Request) -> web.Response:
    return web.Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server(bot: Bot) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_post("/order/{site_id}", handle_order)
    app.router.add_options("/order/{site_id}", handle_order_options)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check + orders webhook server started on port %s", PORT)
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
        voice_task,
        kitchen,
        worktime,
        ai_planner,
        morning_plan,
        evening_plan,
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
        shops,
        shop_templates,
        shop_articles,
        shop_threads,
        posts,
        github_deploy,
        ai_developer,
        website_builder,
        settings as settings_handlers,
    )

    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(voice_task.router)
    dp.include_router(kitchen.router)
    dp.include_router(worktime.router)
    dp.include_router(ai_planner.router)
    dp.include_router(morning_plan.router)
    dp.include_router(evening_plan.router)
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
    dp.include_router(shops.router)
    dp.include_router(shop_templates.router)
    dp.include_router(shop_articles.router)
    dp.include_router(shop_threads.router)
    dp.include_router(posts.router)
    dp.include_router(github_deploy.router)
    dp.include_router(ai_developer.router)
    dp.include_router(website_builder.router)
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

    health_runner = await start_health_server(bot)

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