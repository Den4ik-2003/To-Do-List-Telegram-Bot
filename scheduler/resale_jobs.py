import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import RESALE_CHECK_INTERVAL_MINUTES
from database import resale as resale_db
from services import olx_service, resale_service

logger = logging.getLogger("tasks_bot")


async def check_all_resale_items(bot: Bot):
    saved = await resale_db.get_all_saved()
    for item in saved:
        try:
            price_data = await olx_service.fetch_listing_price(item["url"])
            if not price_data:
                continue
            new_price, currency = price_data
            old_price = item.get("purchase_price")
            if old_price is None or new_price == old_price:
                continue

            await resale_db.update_saved_price(item["_id"], new_price)

            if new_price < old_price:
                await bot.send_message(
                    item["uid"],
                    f"📉 *Ціна змінилась*\n\n{item.get('title', '')}\n\n"
                    f"Було: {old_price:.0f} {currency}\n"
                    f"Стало: {new_price:.0f} {currency}\n\n"
                    f"🔥 Тепер ця можливість цікавіша.\n🔗 {item['url']}",
                )
        except Exception:
            logger.exception("resale monitoring failed for item=%s", item.get("_id"))


def register_resale_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_resale_items,
        "interval",
        minutes=RESALE_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="resale_check",
        replace_existing=True,
    )