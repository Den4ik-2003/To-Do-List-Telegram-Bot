import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import OLX_CHECK_INTERVAL_MINUTES
from database import olx as olx_db
from services import olx_service

logger = logging.getLogger("tasks_bot")


async def _check_listing(bot: Bot, tracker: dict):
    url = tracker.get("url")
    old_price = tracker.get("last_price")
    price_data = await olx_service.fetch_listing_price(url)
    if not price_data:
        return

    new_price, currency = price_data
    if old_price is None:
        await olx_db.update_listing_price(tracker["_id"], new_price)
        return

    if new_price < old_price:
        diff = new_price - old_price
        await bot.send_message(
            tracker["uid"],
            f"🔥 *Ціна впала!*\n\n"
            f"🔗 {url}\n\n"
            f"Було: *{old_price:.0f} {currency}*\n"
            f"Стало: *{new_price:.0f} {currency}*\n"
            f"↓ {diff:.0f} {currency}",
        )

    if new_price != old_price:
        await olx_db.update_listing_price(tracker["_id"], new_price)


async def _check_search(bot: Bot, tracker: dict):
    results = await olx_service.search_listings(
        tracker.get("title_query", ""),
        tracker.get("max_price"),
        tracker.get("location", ""),
        tracker.get("radius_km", 0),
    )
    if not results:
        return

    seen_ids = set(tracker.get("seen_ids", []))
    new_items = [r for r in results if r["id"] not in seen_ids]

    if not seen_ids:
        await olx_db.update_search_seen_ids(tracker["_id"], [r["id"] for r in results])
        return

    if not new_items:
        return

    for item in new_items[:5]:
        price_text = f"{item['price']:.0f} {item['currency']}" if item.get("price") else "ціна не вказана"
        await bot.send_message(
            tracker["uid"],
            f"🆕 *Знайдено новий варіант*\n\n"
            f"{item['title']}\n"
            f"💵 {price_text}\n"
            f"📍 {item.get('location_text', '')}\n\n"
            f"[Відкрити]({item['url']})",
        )

    all_ids = seen_ids | {r["id"] for r in results}
    await olx_db.update_search_seen_ids(tracker["_id"], list(all_ids))


async def check_all_olx_trackers(bot: Bot):
    trackers = await olx_db.get_all_trackers()
    for t in trackers:
        try:
            if t.get("type") == "listing":
                await _check_listing(bot, t)
            elif t.get("type") == "search":
                await _check_search(bot, t)
        except Exception:
            logger.exception("OLX tracker check failed for tracker=%s", t.get("_id"))


def register_olx_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_olx_trackers,
        "interval",
        minutes=OLX_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="olx_check",
        replace_existing=True,
    )