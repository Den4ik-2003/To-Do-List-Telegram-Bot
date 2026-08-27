import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import JOB_CHECK_INTERVAL_MINUTES
from database import jobs as jobs_db
from services import jobs_service

logger = logging.getLogger("tasks_bot")


async def _check_watch(bot: Bot, watch: dict):
    criteria = watch.get("criteria", {})
    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return

    seen_ids = set(watch.get("seen_ids", []))
    new_items = [v for v in vacancies if v["id"] not in seen_ids]

    if new_items:
        for v in new_items[:5]:
            text = (
                f"🆕 *Нова вакансія*\n\n"
                f"💼 {v.get('title','')}\n"
                f"🏢 {v.get('company') or '—'}\n"
                f"🔗 {v.get('url','')}\n"
                f"_(джерело: {v.get('source','')})_"
            )
            await bot.send_message(watch["uid"], text)

    all_ids = seen_ids | {v["id"] for v in vacancies}
    await jobs_db.update_watch_seen(watch["_id"], list(all_ids))


async def check_all_job_watches(bot: Bot):
    watches = await jobs_db.get_all_watches()
    for w in watches:
        try:
            await _check_watch(bot, w)
        except Exception:
            logger.exception("Job watch check failed for watch=%s", w.get("_id"))


def register_jobs_watch_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_job_watches,
        "interval",
        minutes=JOB_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="jobs_watch_check",
        replace_existing=True,
    )