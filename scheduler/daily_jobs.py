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
)
from database import mongo as m
from database import tasks as tasks_db
from database.mongo import db_call
from database.tasks import get_user_tasks, update_task
from database.users import get_user_state, save_user_state, get_all_uids, update_streak
from services import ai_service
from services.planner_service import generate_daily_plan, generate_daily_analysis
from utils.dates import parse_due
from utils.formatting import build_daily_summary_text, fmt_ai_plan_preview
from keyboards.ai import ikb_ai_plan_preview
from keyboards.tasks import ikb_rollover_actions, ikb_reminder_actions
from keyboards.settings import ikb_archive_clear
from handlers.common import compute_daily_stats

# Кеш AI-планів на сьогодні (ранковий автоплан), щоб handlers/ai_planner.py
# міг підхопити той самий план, коли користувач тисне кнопки "toggle"/"додати"
ai_suggestions_cache: dict = {}

logger = logging.getLogger("scheduler.daily_jobs")


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
    if not AI_DAILY_PLAN_ENABLED or not ai_service.is_available():
        logger.info("Автоматичний AI ранковий план вимкнено (немає ключа або AI_DAILY_PLAN_ENABLED=false)")
        return
    while True:
        try:
            hh, mm = map(int, AI_DAILY_PLAN_TIME.split(":"))
        except ValueError:
            hh, mm = 9, 0
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
                    if not state.get("ai_morning_enabled", True):
                        continue
                    if state.get("last_ai_plan_date") == today_str:
                        continue
                    await save_user_state(uid, {"last_ai_plan_date": today_str})

                    plan = await generate_daily_plan(uid)
                    if not plan or not plan.get("tasks"):
                        continue
                    selected = set(range(len(plan["tasks"])))
                    ai_suggestions_cache[uid] = {"plan": plan, "selected": selected}
                    intro = "☀️ *Доброго ранку!*\n\n" + fmt_ai_plan_preview(plan, selected)
                    await bot.send_message(
                        uid, intro,
                        reply_markup=ikb_ai_plan_preview(plan["tasks"], selected),
                    )
                except Exception:
                    logger.exception("ai_morning_plan_task failed for uid %s", uid)
        except Exception:
            logger.exception("ai_morning_plan_task outer loop failed")


def register_scheduler_jobs(bot: Bot):
    asyncio.create_task(reminder_task(bot))
    asyncio.create_task(midnight_rollover_task(bot))
    asyncio.create_task(daily_job_task(bot))
    asyncio.create_task(ai_morning_plan_task(bot))