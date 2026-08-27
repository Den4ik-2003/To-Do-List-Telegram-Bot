import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import SITE_CHECK_INTERVAL_MINUTES
from database import site_watch as site_watch_db
from services import site_watch_service

logger = logging.getLogger("tasks_bot")


async def _check_watch(bot: Bot, watch: dict):
    url = watch.get("url")
    if not url:
        return

    is_up = await site_watch_service.check_site(url)
    prev_status = watch.get("last_status")

    if prev_status is None:
        # Перша перевірка після додавання — фіксуємо стан без алерту
        down_since = None if is_up else datetime.now().isoformat()
        await site_watch_db.update_watch_status(watch["_id"], is_up, down_since)
        return

    if prev_status is True and not is_up:
        down_since = datetime.now().isoformat()
        await site_watch_db.update_watch_status(watch["_id"], False, down_since)
        await bot.send_message(watch["uid"], f"🔴 `{url}` недоступний.")

    elif prev_status is False and is_up:
        down_since_raw = watch.get("down_since")
        duration_text = ""
        if down_since_raw:
            try:
                down_since_dt = datetime.fromisoformat(down_since_raw)
                minutes = int((datetime.now() - down_since_dt).total_seconds() // 60)
                duration_text = f" (був недоступний ~{minutes} хв)"
            except ValueError:
                pass
        await site_watch_db.update_watch_status(watch["_id"], True, None)
        await bot.send_message(watch["uid"], f"🟢 `{url}` знову доступний{duration_text}.")


async def check_all_site_watches(bot: Bot):
    watches = await site_watch_db.get_all_watches()
    for w in watches:
        try:
            await _check_watch(bot, w)
        except Exception:
            logger.exception("Site watch check failed for watch=%s", w.get("_id"))


def register_site_watch_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_site_watches,
        "interval",
        minutes=SITE_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="site_watch_check",
        replace_existing=True,
    )