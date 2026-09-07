import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import (
    SITE_CHECK_INTERVAL_MINUTES, QA_CHECK_INTERVAL_HOURS, QA_MAX_PAGES,
    PAGE_CHECK_INTERVAL_MINUTES, AI_WEEKLY_DIGEST_ENABLED,
    AI_WEEKLY_DIGEST_DAY, AI_WEEKLY_DIGEST_HOUR, AI_WEEKLY_DIGEST_MINUTE,
)
from database import site_watch as site_watch_db
from services import site_watch_service, notification_service

logger = logging.getLogger("tasks_bot")


# ---------------- Uptime-моніторинг (без змін) ----------------

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
        if w.get("kind", "uptime") != "uptime":
            continue
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

    if not is_ok:
        text = site_watch_service.format_qa_report(report)
        await bot.send_message(watch["uid"], f"🧪 *Плановий QA виявив проблеми:*\n\n{text}")


async def run_scheduled_qa(bot: Bot):
    watches = await site_watch_db.get_all_watches()
    for w in watches:
        if w.get("kind", "uptime") != "uptime":
            continue
        try:
            await _run_qa_for_watch(bot, w)
        except Exception:
            logger.exception("Scheduled QA failed for watch=%s", w.get("_id"))


# ---------------- НОВЕ: Моніторинг сторінок (контент-діф + AI) ----------------

def _is_due(watch: dict) -> bool:
    """Перевіряє, чи настав час для перевірки саме цієї сторінки, з
    урахуванням її ІНДИВІДУАЛЬНОЇ частоти (check_interval_minutes)."""
    last = watch.get("last_content_checked_at")
    if not last:
        return True
    interval = watch.get("check_interval_minutes") or PAGE_CHECK_INTERVAL_MINUTES
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now() >= last_dt + timedelta(minutes=interval)


async def _check_page_watch(bot: Bot, watch: dict):
    url = watch.get("url")
    if not url:
        return

    result = await site_watch_service.fetch_page_content_text(url)

    if result is None or result.get("error"):
        became_unreachable = await site_watch_db.set_fetch_status(watch["_id"], False)
        if became_unreachable:
            await bot.send_message(
                watch["uid"],
                f"⚠️ Сторінка недоступна для моніторингу: `{url}`\n"
                f"Причина: {(result or {}).get('error', 'невідома помилка')}",
            )
        return

    new_text = result["text"]
    new_hash = site_watch_service.hash_content(new_text)
    old_hash = watch.get("last_content_hash")
    old_text = watch.get("last_content_text") or ""

    if old_hash is None:
        # перша перевірка — нема з чим порівнювати, просто зберігаємо знімок
        await site_watch_db.update_content_snapshot(watch["_id"], new_hash, new_text)
        return

    if new_hash == old_hash:
        # контент ідентичний — оновлюємо лічильник перевірок, AI НЕ викликаємо
        await site_watch_db.update_content_snapshot(watch["_id"], new_hash, new_text)
        return

    # хеш відрізняється -> є сенс витратити AI-запит на аналіз
    analysis = await site_watch_service.analyze_page_change(url, old_text, new_text)
    await site_watch_db.update_content_snapshot(watch["_id"], new_hash, new_text)

    if not analysis or not analysis.get("is_real_change"):
        return  # AI визначив як технічний шум — нічого не зберігаємо і не шлемо

    importance = analysis.get("importance", "low")
    await site_watch_db.save_page_change(
        watch["_id"], watch["uid"], url,
        importance=importance,
        change_type=analysis.get("change_type", "other"),
        summary=analysis.get("summary", ""),
        before=analysis.get("before", ""),
        after=analysis.get("after", ""),
        why=analysis.get("why_important", ""),
    )

    if site_watch_service.should_notify(watch, importance):
        text = site_watch_service.format_change_notification(url, watch.get("label", url), importance, analysis)
        await notification_service.safe_send(bot, watch["uid"], text)


async def check_all_page_watches(bot: Bot):
    watches = await site_watch_db.get_watches_by_kind("page")
    due = [w for w in watches if _is_due(w)]
    for w in due:
        try:
            await _check_page_watch(bot, w)
        except Exception:
            logger.exception("Page watch check failed for watch=%s", w.get("_id"))


async def send_weekly_digest(bot: Bot):
    """Тижневий AI-звіт про важливі/помірні зміни по всіх сторінках користувача."""
    watches = await site_watch_db.get_watches_by_kind("page")
    week_ago = datetime.now() - timedelta(days=7)

    by_uid: dict[int, list[dict]] = {}
    for w in watches:
        by_uid.setdefault(w["uid"], []).append(w)

    for uid, user_watches in by_uid.items():
        summary_lines = []
        for w in user_watches:
            history = await site_watch_db.get_page_history(w["_id"], limit=10)
            for h in history:
                if h.get("importance") not in ("high", "medium"):
                    continue
                try:
                    checked_dt = datetime.fromisoformat(h.get("checked_at", ""))
                except ValueError:
                    continue
                if checked_dt < week_ago:
                    continue
                label = w.get("label", w["url"])
                summary_lines.append(f"• {label}: {h.get('summary', '')}")

        if not summary_lines:
            continue

        text = "📬 *Тижневий AI-звіт по моніторингу сторінок*\n\n" + "\n".join(summary_lines[:20])
        await notification_service.safe_send(bot, uid, text)


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

    # Перевіряємо готовність кожні 15 хв (найменша з опцій частоти для
    # сторінок) — _is_due() всередині відфільтровує, кому реально пора.
    scheduler.add_job(
        check_all_page_watches,
        "interval",
        minutes=15,
        args=[bot],
        id="site_page_watch_check",
        replace_existing=True,
    )

    if AI_WEEKLY_DIGEST_ENABLED:
        scheduler.add_job(
            send_weekly_digest,
            "cron",
            day_of_week=AI_WEEKLY_DIGEST_DAY,
            hour=AI_WEEKLY_DIGEST_HOUR,
            minute=AI_WEEKLY_DIGEST_MINUTE,
            args=[bot],
            id="site_watch_weekly_digest",
            replace_existing=True,
        )