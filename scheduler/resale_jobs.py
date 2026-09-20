

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import resale as resale_db
from services import resale_service

logger = logging.getLogger("tasks_bot")


async def _collect(monitor: dict) -> dict:
    res = await resale_service.scan_monitor(monitor)
    logger.info(
        "resale collect monitor=%s: scanned=%s analyzed=%s selected=%s error=%s",
        monitor.get("_id"), res["scanned"], res["analyzed"], res["selected"], res["error"],
    )
    return res


async def _process_midday(monitor: dict):
    await _collect(monitor)


async def _process_evening(bot: Bot, monitor: dict):
    error = None
    try:
        await resale_service.recheck_candidates(monitor)
    except Exception:
        logger.exception("resale: recheck упав для monitor=%s", monitor.get("_id"))
    try:
        res = await _collect(monitor)
        error = res.get("error")
    except Exception:
        logger.exception("resale: вечірній збір упав для monitor=%s", monitor.get("_id"))
    try:
        await resale_service.send_report(bot, monitor, error=error)
    except Exception:
        logger.exception("resale: не вдалось надіслати звіт uid=%s", monitor.get("uid"))


async def _run_phase(bot: Bot, phase: str):
    monitors = await resale_db.get_all_active_monitors()
    logger.info("resale %s: активних автопошуків %s", phase, len(monitors))
    for m in monitors:
        mid = str(m["_id"])
        if not resale_service.try_acquire(mid):
            continue  # цей автопошук саме зараз виконується вручну
        try:
            if phase == "midday":
                await _process_midday(m)
            else:
                await _process_evening(bot, m)
        except Exception:
            logger.exception("resale %s: збій автопошуку monitor=%s", phase, mid)
        finally:
            resale_service.release(mid)
    if phase == "evening":
        await resale_db.cleanup_candidates(14)


async def midday_resale_scan(bot: Bot):
    await _run_phase(bot, "midday")


async def evening_resale_report(bot: Bot):
    await _run_phase(bot, "evening")


def register_resale_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    (mh, mm), (eh, em) = resale_service.schedule_times()
    scheduler.add_job(
        midday_resale_scan, CronTrigger(hour=mh, minute=mm), args=[bot],
        id="resale_midday", replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )
    scheduler.add_job(
        evening_resale_report, CronTrigger(hour=eh, minute=em), args=[bot],
        id="resale_evening", replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )