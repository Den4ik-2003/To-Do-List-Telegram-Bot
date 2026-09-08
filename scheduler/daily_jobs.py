import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config.constants import LABELS, STATUS_PENDING, STATUS_DONE
from config.settings import (
    REMINDER_BEFORE_MINUTES,
    DAILY_REPORT_TIME,
    AI_DAILY_PLAN_TIME,
    AI_DAILY_PLAN_ENABLED,
    CURRENCY_UPDATE_TIME,
    WEATHER_MORNING_TIME,
)
from database import mongo as m
from database import tasks as tasks_db
from database.mongo import db_call
from database.tasks import get_user_tasks, update_task
from database.users import get_user_state, save_user_state, get_all_uids, update_streak
from services import ai_service
from services import currency_service
from services import weather_service
from services.planner_service import generate_daily_analysis
from services.insights_service import detect_stalled_goal, generate_insight_text
from utils.dates import parse_due
from utils.formatting import build_daily_summary_text
from keyboards.ai import ikb_insight_actions
from keyboards.tasks import ikb_rollover_actions, ikb_reminder_actions
from keyboards.settings import ikb_archive_clear
from handlers.common import compute_daily_stats

ai_suggestions_cache: dict = {}

logger = logging.getLogger("scheduler.daily_jobs")

INSIGHT_CHECK_INTERVAL_DAYS = 7
INSIGHT_CHECK_TIME = "12:00"

_background_tasks: set[asyncio.Task] = set()


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Фонова задача планувальника впала: %r", exc, exc_info=exc)


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_task_exception)
    return task


async def reminder_task(bot: Bot):
    while True:
        await asyncio.sleep(30)
        try:
            cursor = m.tasks_col.find({"status": STATUS_PENDING, "reminded_before": False}, {"_id": 0})
            pending = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
            now = datetime.now()
            for t in pending:
                due = parse_due(t.get("due", ""))
                if not due:
                    continue
                if due > now and due - now <= timedelta(minutes=REMINDER_BEFORE_MINUTES):
                    text = (
                        f"⏰ *Нагадування!*\n\n"
                        f"Через {REMINDER_BEFORE_MINUTES} хв: *{t.get('text','')}*\n"
                        f"{LABELS.get(t.get('label','idea'),{}).get('emoji','')} "
                        f"{LABELS.get(t.get('label','idea'),{}).get('name','')}\n"
                        f"🕐 {t.get('due','')}"
                    )
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_reminder_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send pre-reminder for task %s", t.get("id"))
                    await update_task(t["id"], {"reminded_before": True})
        except Exception:
            logger.exception("reminder_task loop failed")


async def midnight_rollover_task(bot: Bot):
    while True:
        now = datetime.now()
        target = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            cursor = m.tasks_col.find({"status": STATUS_PENDING}, {"_id": 0})
            pending = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
            today_date = datetime.now().date()
            for t in pending:
                due = parse_due(t.get("due", ""))
                if not due:
                    continue
                if due.date() < today_date:
                    updates = {"missed_flagged": True}
                    if not t.get("missed_counted"):
                        updates["missed_counted"] = True
                        state = await get_user_state(t["uid"])
                        await save_user_state(t["uid"], {"total_missed": state.get("total_missed", 0) + 1})
                    await update_task(t["id"], updates)
                    text = (
                        f"❌ *Не встигли зробити вчасно*\n\n"
                        f"\"{t.get('text','')}\"\n"
                        f"🕐 Було заплановано: {t.get('due','')}\n\n"
                        f"Перенести?"
                    )
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_rollover_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send rollover notice for task %s", t.get("id"))
        except Exception:
            logger.exception("midnight_rollover_task failed")


async def daily_job_task(bot: Bot):
    while True:
        try:
            hh, mm = map(int, DAILY_REPORT_TIME.split(":"))
        except ValueError:
            hh, mm = 21, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            for uid in uids:
                try:
                    stats = await compute_daily_stats(uid, tasks_db)
                    streak = await update_streak(uid, stats["missed_count"])
                    await bot.send_message(uid, build_daily_summary_text(stats, streak))

                    if ai_service.is_available():
                        try:
                            analysis = await generate_daily_analysis(uid, stats)
                            if analysis:
                                await bot.send_message(uid, f"🌙 *AI Підсумок дня*\n\n{analysis}")
                        except Exception:
                            logger.exception("Вечірній AI-аналіз не вдався для uid %s", uid)

                    cursor = m.tasks_col.find({"uid": uid, "postponed_today": True}, {"_id": 0, "id": 1})
                    postponed_ids = [d["id"] for d in (await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or [])]
                    for tid in postponed_ids:
                        await update_task(tid, {"postponed_today": False})

                    if target.day == 1:
                        state = await get_user_state(uid)
                        month_key = target.strftime("%Y-%m")
                        if state.get("archive_prompt_month") != month_key:
                            done_count = len(await get_user_tasks(uid, statuses=[STATUS_DONE]))
                            if done_count > 0:
                                await bot.send_message(
                                    uid,
                                    f"У вас\n\n{done_count} виконаних задач.\n\nОчистити архів?",
                                    reply_markup=ikb_archive_clear()
                                )
                            await save_user_state(uid, {"archive_prompt_month": month_key})
                except Exception:
                    logger.exception("daily_job_task failed for uid %s", uid)
        except Exception:
            logger.exception("daily_job_task outer loop failed")


async def ai_morning_plan_task(bot: Bot):
    while True:
        if not AI_DAILY_PLAN_ENABLED or not ai_service.is_available():
            logger.warning("ai_morning_plan_task: AI недоступний (ключ/фіча вимкнені), перевірю знову через 30 хв")
            await asyncio.sleep(1800)
            continue
        try:
            hh, mm = map(int, AI_DAILY_PLAN_TIME.split(":"))
        except ValueError:
            hh, mm = 9, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        logger.info("ai_morning_plan_task: сплю до %s (локальний час сервера)", target.isoformat())
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            today_str = datetime.now().strftime("%Y-%m-%d")
            logger.info("ai_morning_plan_task: старт розсилки для %d користувачів", len(uids))
            sent = 0
            for uid in uids:
                try:
                    state = await get_user_state(uid)
                    if not state.get("ai_morning_enabled", True):
                        continue
                    if state.get("last_ai_plan_date") == today_str:
                        continue
                    await save_user_state(uid, {
                        "last_ai_plan_date": today_str,
                        "awaiting_morning_time": True,
                        "awaiting_morning_date": today_str,
                    })
                    await bot.send_message(
                        uid,
                        "🌅 *Доброго ранку! Плануємо сьогодні?*\n\n"
                        "Скільки часу ти сьогодні маєш для виконання задач?\n\n"
                        "Напиши, наприклад:\n`3 години`\nабо\n`2 години, з 19:00 до 21:00`",
                    )
                    sent += 1
                except Exception:
                    logger.exception("ai_morning_plan_task failed for uid %s", uid)
            logger.info("ai_morning_plan_task: розіслано ранкове питання %d користувачам", sent)
        except Exception:
            logger.exception("ai_morning_plan_task outer loop failed")


async def proactive_insights_task(bot: Bot):
    while True:
        if not ai_service.is_available():
            logger.info("Проактивні AI-інсайти чекають — немає AI ключа, перевірю через 30 хв")
            await asyncio.sleep(1800)
            continue
        try:
            hh, mm = map(int, INSIGHT_CHECK_TIME.split(":"))
        except ValueError:
            hh, mm = 12, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            today_str = datetime.now().strftime("%Y-%m-%d")
            for uid in uids:
                try:
                    state = await get_user_state(uid)
                    if not state.get("ai_insights_enabled", True):
                        continue
                    last_sent = state.get("last_insight_date")
                    if last_sent:
                        try:
                            days_since = (datetime.now() - datetime.strptime(last_sent, "%Y-%m-%d")).days
                        except ValueError:
                            days_since = INSIGHT_CHECK_INTERVAL_DAYS
                        if days_since < INSIGHT_CHECK_INTERVAL_DAYS:
                            continue

                    context = await detect_stalled_goal(uid)
                    if not context:
                        continue

                    text = await generate_insight_text(context)
                    if not text:
                        continue

                    goal = context["goal"]
                    gid = str(goal.get("_id") or goal.get("id") or "")
                    await bot.send_message(
                        uid,
                        f"🧠 {text}",
                        reply_markup=ikb_insight_actions(gid),
                    )
                    await save_user_state(uid, {"last_insight_date": today_str})
                except Exception:
                    logger.exception("proactive_insights_task failed for uid %s", uid)
        except Exception:
            logger.exception("proactive_insights_task outer loop failed")


async def currency_update_task():
    while True:
        try:
            hh, mm = map(int, CURRENCY_UPDATE_TIME.split(":"))
        except ValueError:
            hh, mm = 8, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            result = await currency_service.update_rates()
            if not result:
                logger.warning("currency_update_task: курси не оновлено (порожня відповідь)")
        except Exception:
            logger.exception("currency_update_task failed")


async def weather_morning_task(bot: Bot):
    while True:
        try:
            hh, mm = map(int, WEATHER_MORNING_TIME.split(":"))
        except ValueError:
            hh, mm = 7, 30
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            for uid in uids:
                try:
                    state = await get_user_state(uid)
                    if not state.get("weather_morning_enabled", True):
                        continue
                    lat = state.get("city_lat")
                    lon = state.get("city_lon")
                    display_name = state.get("city_display")
                    if lat is None or lon is None:
                        continue
                    report = await weather_service.build_weather_report(lat, lon, display_name)
                    if report:
                        await bot.send_message(uid, f"☀️ *Доброго ранку!*\n\n{report}")
                except Exception:
                    logger.exception("weather_morning_task failed for uid %s", uid)
        except Exception:
            logger.exception("weather_morning_task outer loop failed")


def register_scheduler_jobs(bot: Bot):
    _spawn(reminder_task(bot), "reminder_task")
    _spawn(midnight_rollover_task(bot), "midnight_rollover_task")
    _spawn(daily_job_task(bot), "daily_job_task")
    _spawn(ai_morning_plan_task(bot), "ai_morning_plan_task")
    _spawn(proactive_insights_task(bot), "proactive_insights_task")
    _spawn(currency_update_task(), "currency_update_task")
    _spawn(weather_morning_task(bot), "weather_morning_task")
    logger.info("Зареєстровано %d фонових задач планувальника, посилання збережено (захист від GC)", len(_background_tasks))