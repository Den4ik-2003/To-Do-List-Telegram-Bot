"""
ЗМІНЕНИЙ ФАЙЛ: scheduler/jobs_watch_jobs.py

ПОВНІСТЮ ПЕРЕРОБЛЕНО під фічу "🌙 Автопошук вакансій двічі на день":
- Раніше: один interval-джоб раз на JOB_CHECK_INTERVAL_MINUTES, який
  одразу слав окреме повідомлення про кожну нову вакансію (без AI-скору,
  без кнопок відгуку).
- Тепер: два cron-джоби (обід і вечір, час з config.settings.
  JOB_AUTOSEARCH_NOON_TIME/JOB_AUTOSEARCH_EVENING_TIME):
  - run_autosearches_noon(): шукає, рахує AI-скор, накопичує знахідки в
    pending_digest КОЖНОГО автопошуку (database.jobs.append_pending_digest)
    і ОНОВЛЮЄ seen_ids — але НІЧОГО не шле користувачу.
  - run_autosearches_evening(): шукає ще раз (щоб зловити те, що
    з'явилось після обіду), додає нові знахідки в pending_digest, потім
    дістає й ОЧИЩАЄ pending_digest кожного автопошуку (database.jobs.
    get_and_clear_pending_digest) — це і є накопичене за ВЕСЬ день. Групує
    по uid (у користувача може бути кілька активних автопошуків) і
    викликає handlers.jobs.send_autosearch_digest(), яка формує підсумок
    і надсилає інтерактивні картки з кнопкою "📨 Відгукнутися".
"""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import JOB_AUTOSEARCH_NOON_TIME, JOB_AUTOSEARCH_EVENING_TIME
from database import job_profile as job_profile_db
from database import jobs as jobs_db
from services import jobs_service

logger = logging.getLogger("tasks_bot")


def _parse_hm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except Exception:
        logger.warning("Не вдалося розпарсити час %r, використовую %02d:%02d за замовчуванням", value, *fallback)
        return fallback


async def _run_watch_cycle(watch: dict, profile: dict | None) -> list[dict]:
    """Виконує пошук для ОДНОГО автопошуку, оновлює seen_ids, повертає
    НОВІ (ще не бачені) вакансії зі скором відповідності, відсортовані за
    match_percent спадно."""
    criteria = watch.get("criteria", {})
    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return []

    seen_ids = set(watch.get("seen_ids", []))
    new_items = [v for v in vacancies if v["id"] not in seen_ids]

    scored_new = []
    for v in new_items:
        v["_score"] = await jobs_service.score_vacancy(v, profile)
        scored_new.append(v)
    scored_new.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

    all_ids = seen_ids | {v["id"] for v in vacancies}
    await jobs_db.update_watch_seen(watch["_id"], list(all_ids))

    return scored_new


async def run_autosearches_noon(bot: Bot):
    """Обідній прогін: шукає й накопичує знахідки в pending_digest.
    НІЧОГО не шле користувачу — все піде в один вечірній підсумок."""
    watches = await jobs_db.get_all_watches()
    logger.info("Обідній автопошук: перевіряю %s активних автопошуків", len(watches))
    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w["uid"])
            new_items = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w["_id"], new_items)
            logger.info(
                "Автопошук %s (uid=%s): знайдено %s нових вакансій (обід)",
                w["_id"], w["uid"], len(new_items),
            )
        except Exception:
            logger.exception("Обідній автопошук впав для watch=%s", w.get("_id"))


async def run_autosearches_evening(bot: Bot):
    """Вечірній прогін: шукає ще раз, зливає з обідніми знахідками
    (pending_digest), формує й надсилає ОДИН підсумок на юзера з усіма
    активними автопошуками — гарантовано з хоча б однією повною карткою
    вакансії, якщо щось знайдено за день."""
    from handlers import jobs as jobs_handler  # локальний імпорт — без циклічних залежностей при старті бота

    watches = await jobs_db.get_all_watches()
    logger.info("Вечірній автопошук: перевіряю %s активних автопошуків", len(watches))

    by_uid: dict[int, list[dict]] = {}

    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w["uid"])
            new_items = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w["_id"], new_items)

            pending = await jobs_db.get_and_clear_pending_digest(w["_id"])
            for v in pending:
                if "_score" not in v:
                    v["_score"] = await jobs_service.score_vacancy(v, profile)
            pending.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

            title = w.get("title") or w.get("criteria", {}).get("profession") or "Без назви"
            by_uid.setdefault(w["uid"], []).append({"title": title, "vacancies": pending})
        except Exception:
            logger.exception("Вечірній автопошук впав для watch=%s", w.get("_id"))

    for uid, searches in by_uid.items():
        try:
            await jobs_handler.send_autosearch_digest(bot, uid, searches)
        except Exception:
            logger.exception("Не вдалося надіслати вечірній дайджест uid=%s", uid)


def register_jobs_watch_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    noon_h, noon_m = _parse_hm(JOB_AUTOSEARCH_NOON_TIME, (13, 0))
    evening_h, evening_m = _parse_hm(JOB_AUTOSEARCH_EVENING_TIME, (19, 0))

    scheduler.add_job(
        run_autosearches_noon,
        CronTrigger(hour=noon_h, minute=noon_m),
        args=[bot],
        id="jobs_autosearch_noon",
        replace_existing=True,
    )
    scheduler.add_job(
        run_autosearches_evening,
        CronTrigger(hour=evening_h, minute=evening_m),
        args=[bot],
        id="jobs_autosearch_evening",
        replace_existing=True,
    )
    logger.info(
        "Автопошук вакансій заплановано: обід %02d:%02d, вечір %02d:%02d",
        noon_h, noon_m, evening_h, evening_m,
    )