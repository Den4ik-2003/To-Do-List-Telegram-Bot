import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import SITE_CHECK_INTERVAL_MINUTES, QA_CHECK_INTERVAL_HOURS, QA_MAX_PAGES
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


async def _run_qa_for_watch(bot: Bot, watch: dict):
    url = watch.get("url")
    if not url:
        return
    try:
        report = await site_watch_service.run_qa_scan(url, max_pages=QA_MAX_PAGES)
    except Exception:
        logger.exception("Scheduled QA scan failed for %s", url)
        return

    is_ok = not report.get("critical_error") and not report.get("broken_pages") and \
        not report.get("form_issues") and not report.get("broken_images")

    await site_watch_db.save_qa_result(watch["_id"], watch["uid"], url, report, is_ok)

    # Повідомляємо тільки якщо є проблеми — щоб не спамити "все ок" щодня
    if not is_ok:
        text = site_watch_service.format_qa_report(report)
        await bot.send_message(watch["uid"], f"🧪 *Плановий QA виявив проблеми:*\n\n{text}")


async def run_scheduled_qa(bot: Bot):
    watches = await site_watch_db.get_all_watches()
    for w in watches:
        try:
            await _run_qa_for_watch(bot, w)
        except Exception:
            logger.exception("Scheduled QA failed for watch=%s", w.get("_id"))


def register_site_watch_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_site_watches,
        "interval",
        minutes=SITE_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="site_watch_check",
        replace_existing=True,
    )
    scheduler.add_job(
        run_scheduled_qa,
        "interval",
        hours=QA_CHECK_INTERVAL_HOURS,
        args=[bot],
        id="site_qa_check",
        replace_existing=True,
    )