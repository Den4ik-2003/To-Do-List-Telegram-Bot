# scheduler/jobs_watch_jobs.py
import logging
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config.settings import JOB_AUTOSEARCH_NOON_TIME, JOB_AUTOSEARCH_EVENING_TIME, JOB_MIN_MATCH_PERCENT
from database import job_profile as job_profile_db
from database import jobs as jobs_db
from services import jobs_service
logger = logging.getLogger('tasks_bot')

def _parse_hm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = value.strip().split(':')
        return (int(h), int(m))
    except Exception:
        logger.warning('Не вдалося розпарсити час %r, використовую %02d:%02d за замовчуванням', value, *fallback)
        return fallback

def _match_percent(vacancy: dict) -> float | None:
    raw = (vacancy.get('_score') or {}).get('match_percent')
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

def _is_good_match(vacancy: dict) -> bool:
    pct = _match_percent(vacancy)
    return pct is not None and pct > JOB_MIN_MATCH_PERCENT

async def _run_watch_cycle(watch: dict, profile: dict | None) -> list[dict]:
    criteria = watch.get('criteria', {})
    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return []
    seen_ids = set(watch.get('seen_ids', []))
    new_items = [v for v in vacancies if v['id'] not in seen_ids]
    good: list[dict] = []
    unscored_ids: set[str] = set()
    for v in new_items:
        v['_score'] = await jobs_service.score_vacancy(v, profile)
        if _match_percent(v) is None:
            unscored_ids.add(v['id'])
        elif _is_good_match(v):
            good.append(v)
    good.sort(key=lambda v: _match_percent(v) or 0, reverse=True)
    logger.info('Автопошук %s: нових %s, збіг > %s%%: %s, без оцінки (повторю пізніше): %s', watch.get('_id'), len(new_items), JOB_MIN_MATCH_PERCENT, len(good), len(unscored_ids))
    all_ids = (seen_ids | {v['id'] for v in vacancies}) - unscored_ids
    await jobs_db.update_watch_seen(watch['_id'], list(all_ids))
    return good

async def run_autosearches_noon(bot: Bot):
    watches = await jobs_db.get_all_watches()
    logger.info('Обідній автопошук: перевіряю %s активних автопошуків', len(watches))
    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w['uid'])
            new_items = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w['_id'], new_items)
            logger.info('Автопошук %s (uid=%s): підходящих нових вакансій %s (обід)', w['_id'], w['uid'], len(new_items))
        except Exception:
            logger.exception('Обідній автопошук впав для watch=%s', w.get('_id'))

async def _maybe_send_profile_hint(bot: Bot, uid: int, profile: dict | None):
    try:
        if not job_profile_db.should_send_profile_hint(profile):
            return
        status = job_profile_db.get_profile_status(profile)
        await bot.send_message(uid, f'💡 Профіль для підбору вакансій заповнено на {status['percent']}%.\n\nЩо більше я знаю про тебе, то точніше рахую збіг і то кращі вакансії показую.\nВідкрий «👤 Мої дані для пошуку» → «➕ Доповнити порожні».')
        await job_profile_db.mark_profile_hint_sent(uid)
    except Exception:
        logger.exception('Не вдалося надіслати підказку про профіль uid=%s', uid)

async def run_autosearches_evening(bot: Bot):
    from handlers import jobs as jobs_handler
    watches = await jobs_db.get_all_watches()
    logger.info('Вечірній автопошук: перевіряю %s активних автопошуків', len(watches))
    by_uid: dict[int, list[dict]] = {}
    profiles: dict[int, dict | None] = {}
    for w in watches:
        try:
            profile = await job_profile_db.get_profile(w['uid'])
            profiles[w['uid']] = profile
            new_items = await _run_watch_cycle(w, profile)
            if new_items:
                await jobs_db.append_pending_digest(w['_id'], new_items)
            pending = await jobs_db.get_and_clear_pending_digest(w['_id'])
            matched: list[dict] = []
            for v in pending:
                if '_score' not in v:
                    v['_score'] = await jobs_service.score_vacancy(v, profile)
                if _is_good_match(v):
                    matched.append(v)
            matched.sort(key=lambda v: _match_percent(v) or 0, reverse=True)
            logger.info('Автопошук %s (uid=%s): у накопиченому %s, після фільтра > %s%%: %s', w['_id'], w['uid'], len(pending), JOB_MIN_MATCH_PERCENT, len(matched))
            title = w.get('title') or w.get('criteria', {}).get('profession') or 'Без назви'
            by_uid.setdefault(w['uid'], []).append({'title': title, 'vacancies': matched})
        except Exception:
            logger.exception('Вечірній автопошук впав для watch=%s', w.get('_id'))
    for uid, searches in by_uid.items():
        try:
            await jobs_handler.send_autosearch_digest(bot, uid, searches)
        except Exception:
            logger.exception('Не вдалося надіслати вечірній дайджест uid=%s', uid)
        await _maybe_send_profile_hint(bot, uid, profiles.get(uid))

def register_jobs_watch_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    noon_h, noon_m = _parse_hm(JOB_AUTOSEARCH_NOON_TIME, (13, 0))
    evening_h, evening_m = _parse_hm(JOB_AUTOSEARCH_EVENING_TIME, (19, 0))
    scheduler.add_job(run_autosearches_noon, CronTrigger(hour=noon_h, minute=noon_m), args=[bot], id='jobs_autosearch_noon', replace_existing=True)
    scheduler.add_job(run_autosearches_evening, CronTrigger(hour=evening_h, minute=evening_m), args=[bot], id='jobs_autosearch_evening', replace_existing=True)
    logger.info('Автопошук вакансій заплановано: обід %02d:%02d, вечір %02d:%02d', noon_h, noon_m, evening_h, evening_m)