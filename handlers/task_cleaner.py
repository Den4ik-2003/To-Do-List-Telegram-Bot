"""
handlers/task_cleaner.py

Обробники кнопок тижневого дайджесту "🧹 AI-прибиральник"
(scheduler/daily_jobs.py:ai_cleaner_task надсилає повідомлення з
ikb_cleaner_actions()).

cleaner_digest_cache — кеш task_id-ів застарілих задач по uid, той самий
патерн, що rollover_digest_cache у handlers/tasks.py: наповнює
scheduler/daily_jobs.py, читають хендлери нижче.

"📋 Обрати самому" перевикористовує вже наявний ikb_tasks_list/
user_list_cache (як resched_pick_cb у handlers/tasks.py) — окремого
UI для видалення НЕ робив: користувач відкриває картку конкретної
задачі і тисне вже наявну там кнопку "🗑 Видалити" (deltask:).
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from config.constants import DB_ERROR_TEXT
from database import tasks as tasks_db
from services import task_cleaner_service
from handlers.common import user_list_cache
from keyboards.tasks import ikb_tasks_list

logger = logging.getLogger("tasks_bot")
router = Router(name="task_cleaner")

# НАПОВНЮЄ: scheduler/daily_jobs.py (ai_cleaner_task)
# ЧИТАЮТЬ: хендлери нижче
cleaner_digest_cache: dict[int, list[int]] = {}


@router.callback_query(F.data == "cleaner_delete_all")
async def cleaner_delete_all_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        task_ids = cleaner_digest_cache.pop(uid, None) or []
        if not task_ids:
            return await cb.answer("Список застарів, спробуй наступного тижня.", show_alert=True)

        deleted = await task_cleaner_service.delete_tasks(task_ids)
        text = f"🗑 Видалено {deleted} задач(і)."
        try:
            await cb.message.edit_text(text)
        except TelegramAPIError:
            await cb.message.answer(text)
        await cb.answer("Готово!")
    except Exception:
        logger.exception("cleaner_delete_all_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "cleaner_pick")
async def cleaner_pick_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        task_ids = cleaner_digest_cache.get(uid) or []
        if not task_ids:
            return await cb.answer("Список застарів, спробуй наступного тижня.", show_alert=True)

        tasks = []
        for tid in task_ids:
            t = await tasks_db.get_task(tid)
            if t:
                tasks.append(t)
        if not tasks:
            return await cb.answer("Список застарів.", show_alert=True)

        user_list_cache[uid] = tasks
        text = "Обери задачу — відкриється картка, де можна її видалити:"
        try:
            await cb.message.edit_text(text, reply_markup=ikb_tasks_list(tasks))
        except TelegramAPIError:
            await cb.message.answer(text, reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("cleaner_pick_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "cleaner_dismiss_all")
async def cleaner_dismiss_all_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        cleaner_digest_cache.pop(uid, None)
        try:
            await cb.message.edit_text("🔕 Залишено як є. Нагадаю знову наступного тижня.")
        except TelegramAPIError:
            pass
        await cb.answer()
    except Exception:
        logger.exception("cleaner_dismiss_all_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass