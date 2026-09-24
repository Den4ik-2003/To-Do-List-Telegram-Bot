"""
ЗМІНЕНИЙ ФАЙЛ: scheduler/jobs_watch_jobs.py

НОВЕ (стійкість до збою / ліміту AI):
- Оцінка вакансій йде ПАЧКАМИ (jobs_service.score_vacancies_batch), а не
  по одній: ~10 AI-запитів замість ~90 за прогін.
- JOB_MAX_SCORE_PER_CYCLE обмежує, скільки нових вакансій оцінюється за
  один прогін одного автопошуку. Решта не позначається переглянутою і
  дочекається наступного прогону.
- Якщо AI не зміг оцінити вакансії й нічого підходящого немає, у вечірній
  підсумок НЕ йде хибне «нічого підходящого сьогодні». Натомість
  користувач отримує окреме повідомлення, що вакансії не оцінено через
  збій/ліміт AI і їх буде оцінено наступного прогону.
- Вакансії з обіднього pending_digest, які не вдалося переоцінити ввечері,
  повертаються в pending_digest, а не губляться.

Раніше:
- До pending_digest і у вечірній підсумок потрапляють лише вакансії, де
  match_percent СТРОГО БІЛЬШИЙ за JOB_MIN_MATCH_PERCENT.
- Неоцінені вакансії не потрапляють у seen_ids (оцінимо наступного разу).
- Раз на PROFILE_HINT_EVERY_DAYS днів нагадування про неповний профіль.
- Два cron-джоби: обід (накопичує) і вечір (шле підсумок).
"""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import (
    JOB_AUTOSEARCH_NOON_TIME,
    JOB_AUTOSEARCH_EVENING_TIME,
    JOB_MIN_MATCH_PERCENT,
    JOB_MAX_SCORE_PER_CYCLE,
)
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


def _match_percent(vacancy: dict) -> float | None:
    """Відсоток збігу з _score вакансії або None, якщо оцінити не вдалося."""
    return jobs_service.get_match_percent(vacancy)


def _is_good_match(vacancy: dict) -> bool:
    pct = _match_percent(vacancy)
    return pct is not None and pct > JOB_MIN_MATCH_PERCENT


async def _run_watch_cycle(watch: dict, profile: dict | None) -> tuple[list[dict], int]:
    """Виконує пошук для ОДНОГО автопошуку, оновлює seen_ids.
    Повертає (good, unscored):
    - good: НОВІ вакансії зі збігом > JOB_MIN_MATCH_PERCENT, за спаданням;
    - unscored: скільки нових вакансій AI не зміг оцінити."""
    criteria = watch.get("criteria", {})
    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return [], 0

    seen_ids = set(watch.get("seen_ids", []))
    new_items = [v for v in vacancies if v["id"] not in seen_ids]

    to_score = new_items[:JOB_MAX_SCORE_PER_CYCLE]
    deferred = new_items[JOB_MAX_SCORE_PER_CYCLE:]

    await jobs_service.score_vacancies_batch(to_score, profile)

    good: list[dict] = []
    unscored_ids: set[str] = set()
    for v in to_score:
        if _match_percent(v) is None:
            unscored_ids.add(v["id"])
        elif _is_good_match(v):
            good.append(v)
    good.sort(key=lambda v: _match_percent(v) or 0, reverse=True)

    logger.info(
        "Автопошук %s: нових %s, збіг > %s%%: %s, без оцінки (повторю пізніше): %s, відкладено (ліміт за прогін): %s",
        watch.get("_id"), len(new_items), JOB_MIN_MATCH_PERCENT, len(good),
        len(unscored_ids), len(deferred),
    )

    # Неоцінені й відкладені вакансії не позначаємо переглянутими —
    # наступний прогін спробує ще раз
    skip_ids = unscored_ids | {v["id"] for v in deferred}
    all_ids = (seen_ids | {v["id"] for v in vacancies}) - skip_ids
    await jobs_db.update_watch_seen(watch["_id"], list(all_ids))

    return good, len(unscored_ids)


async def run_autosearches_noon(bot: Bot):
    """Обідній прогін: шукає й накопичує знахідки в pending_digest.
    НІЧОГО не шле користувачу — все піде в один вечірній підсумок."""
    watches = await jobs_db.get_all_watches()
    logger.info("Обідній автопошук: перевіряю %s активних автопошуків", len(watches))
    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w["uid"])
            new_items, _unscored = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w["_id"], new_items)
            logger.info(
                "Автопошук %s (uid=%s): підходящих нових вакансій %s (обід)",
                w["_id"], w["uid"], len(new_items),
            )
        except Exception:
            logger.exception("Обідній автопошук впав для watch=%s", w.get("_id"))


async def _maybe_send_profile_hint(bot: Bot, uid: int, profile: dict | None):
    """Раз на кілька днів нагадує, що неповний профіль = менш точний збіг."""
    try:
        if not job_profile_db.should_send_profile_hint(profile):
            return
        status = job_profile_db.get_profile_status(profile)
        await bot.send_message(
            uid,
            f"💡 Профіль для підбору вакансій заповнено на {status['percent']}%.\n\n"
            "Що більше я знаю про тебе, то точніше рахую збіг і то кращі вакансії показую.\n"
            "Відкрий «👤 Мої дані для пошуку» → «➕ Доповнити порожні».",
        )
        await job_profile_db.mark_profile_hint_sent(uid)
    except Exception:
        logger.exception("Не вдалося надіслати підказку про профіль uid=%s", uid)


async def run_autosearches_evening(bot: Bot):
    """Вечірній прогін: шукає ще раз, зливає з обідніми знахідками
    (pending_digest), формує й надсилає ОДИН підсумок на юзера.
    У підсумок потрапляють лише вакансії зі збігом > JOB_MIN_MATCH_PERCENT.
    Якщо вакансії не вдалося оцінити через збій AI — користувач отримує
    окреме чесне повідомлення замість «нічого підходящого»."""
    from handlers import jobs as jobs_handler  # локальний імпорт — без циклічних залежностей при старті бота

    watches = await jobs_db.get_all_watches()
    logger.info("Вечірній автопошук: перевіряю %s активних автопошуків", len(watches))

    by_uid: dict[int, list[dict]] = {}
    notices: dict[int, list[str]] = {}
    profiles: dict[int, dict | None] = {}

    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w["uid"])
            profiles[w["uid"]] = profile
            new_items, unscored = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w["_id"], new_items)

            pending = await jobs_db.get_and_clear_pending_digest(w["_id"])

            # Скор у pending_digest міг не зберегтись (або профіль змінився
            # між обідом і вечором) — оцінюємо ті, що без валідної оцінки, пачками.
            to_rescore = [v for v in pending if _match_percent(v) is None]
            if to_rescore:
                await jobs_service.score_vacancies_batch(to_rescore, profile)

            matched: list[dict] = []
            still_unscored: list[dict] = []
            for v in pending:
                if _match_percent(v) is None:
                    still_unscored.append(v)
                elif _is_good_match(v):
                    matched.append(v)
            matched.sort(key=lambda v: _match_percent(v) or 0, reverse=True)

            # Не губимо те, що не вдалося оцінити: повертаємо в pending
            # (вони вже позначені переглянутими, тож дублів не буде)
            if still_unscored:
                await jobs_db.append_pending_digest(w["_id"], still_unscored)

            logger.info(
                "Автопошук %s (uid=%s): у накопиченому %s, після фільтра > %s%%: %s, без оцінки: %s",
                w["_id"], w["uid"], len(pending), JOB_MIN_MATCH_PERCENT, len(matched),
                unscored + len(still_unscored),
            )

            title = w.get("title") or w.get("criteria", {}).get("profession") or "Без назви"
            not_scored_total = unscored + len(still_unscored)

            if matched or not_scored_total == 0:
                by_uid.setdefault(w["uid"], []).append({"title": title, "vacancies": matched})
            else:
                # Нічого підходящого немає, але частину вакансій НЕ оцінено —
                # не кажемо «нічого не знайшлось», а чесно пояснюємо.
                notices.setdefault(w["uid"], []).append(
                    f"🔎 «{title}» — не вдалося оцінити вакансій: {not_scored_total}"
                )
        except Exception:
            logger.exception("Вечірній автопошук впав для watch=%s", w.get("_id"))

    for uid, searches in by_uid.items():
        try:
            await jobs_handler.send_autosearch_digest(bot, uid, searches)
        except Exception:
            logger.exception("Не вдалося надіслати вечірній дайджест uid=%s", uid)

    for uid, lines in notices.items():
        try:
            await bot.send_message(
                uid,
                "⚠️ Вечірній підсумок автопошуку неповний\n\n"
                + "\n".join(lines)
                + "\n\nAI зараз недоступний (імовірно, вичерпано ліміт запитів) "
                  "або профіль порожній. Це не означає, що вакансій немає, "
                  "я оціню їх наступного прогону.",
            )
        except Exception:
            logger.exception("Не вдалося надіслати повідомлення про збій AI uid=%s", uid)

    for uid in set(by_uid) | set(notices):
        await _maybe_send_profile_hint(bot, uid, profiles.get(uid))


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