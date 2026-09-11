"""
scheduler/resale_jobs.py

Фонова перевірка активних AI-моніторингів "🔥 Знайти перепродаж".
Джоба тепер тікає раз на добу (о RESALE_CHECK_TIME) для всіх активних
моніторингів одразу, замість частого interval-тіку з власним
check_interval_minutes у кожного моніторингу.
"""

import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import (
    RESALE_MIN_SCORE_THRESHOLD,
    RESALE_MAX_NOTIFY_PER_CYCLE,
    RESALE_CHECK_TIME,
)
from database import resale as resale_db
from services import resale_service

logger = logging.getLogger("tasks_bot")


def _is_due(monitor: dict) -> bool:
    last = monitor.get("last_checked_at")
    interval = monitor.get("check_interval_minutes") or 180
    if not last:
        return True
    try:
        minutes_since = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
    except ValueError:
        return True
    return minutes_since >= interval


async def _check_monitor(bot: Bot, monitor: dict):
    if not _is_due(monitor):
        return

    try:
        opportunities, error = await resale_service.scan_monitor(monitor)
    except Exception:
        logger.exception("resale monitor scan failed for monitor=%s", monitor.get("_id"))
        return
    finally:
        await resale_db.touch_monitor_checked(monitor["_id"])

    if error or not opportunities:
        return

    urls = []
    sent = 0
    for opp in opportunities:
        url = opp["listing"].get("url")
        if url:
            urls.append(url)
        if opp["score"] < RESALE_MIN_SCORE_THRESHOLD or sent >= RESALE_MAX_NOTIFY_PER_CYCLE:
            continue
        pending_id = resale_service.register_pending(monitor["_id"], opp)
        try:
            await bot.send_message(
                monitor["uid"],
                resale_service.format_notification(opp),
                reply_markup=resale_service.ikb_notification(pending_id, url),
            )
        except Exception:
            logger.exception("resale: не вдалось надіслати сповіщення uid=%s", monitor["uid"])
            continue
        sent += 1

    if urls:
        await resale_db.mark_seen(monitor["_id"], urls)
    await resale_db.increment_stat(monitor["_id"], "found", len(opportunities))


async def check_all_resale_monitors(bot: Bot):
    monitors = await resale_db.get_all_active_monitors()
    for m in monitors:
        try:
            await _check_monitor(bot, m)
        except Exception:
            logger.exception("resale monitor check failed for monitor=%s", m.get("_id"))


def register_resale_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    hour, minute = (int(x) for x in RESALE_CHECK_TIME.split(":"))
    scheduler.add_job(
        check_all_resale_monitors,
        CronTrigger(hour=hour, minute=minute),
        args=[bot],
        id="resale_check",
        replace_existing=True,
    )