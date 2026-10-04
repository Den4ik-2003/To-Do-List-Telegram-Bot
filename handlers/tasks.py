import asyncio
import difflib
import logging
import re
import secrets
from collections import Counter
from datetime import datetime, date, timedelta

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery

from config.constants import (
    LABELS, CATEGORIES, LABEL_XP, STATUS_PENDING, STATUS_DONE,
    DB_ERROR_TEXT,
)
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import users as users_db
from database import projects as projects_db
from services import reschedule_service, ai_service
from utils.dates import now_local, describe_schedule, build_schedule, DEFAULT_TZ
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.tasks import (
    kb_tasks_menu, kb_label, label_from_text, kb_category, category_from_text,
    kb_date, kb_project_select, project_from_text,
    ikb_task_actions, ikb_edit_fields, ikb_tasks_list, ikb_categories,
    ikb_view_day, ikb_recurring_notice, ikb_recurring_move, ikb_recurring_suggestion,
    ikb_rc_schedule, ikb_rc_days, ikb_recurring_list, ikb_recurring_card,
    ikb_recurring_edit, ikb_rc_category, ikb_rc_priority, ikb_rc_confirm_delete,
)
from handlers.common import (
    require_auth, user_list_cache, fmt_task, fmt_due, parse_due,
    is_today, is_missed, sort_tasks, sort_tasks_by_label_then_due, level_progress,
    build_progress_text,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="tasks")

CANCEL_TEXT = "❌ Скасувати"
NO_DUE_TEXT = "⏭ Без терміну"
NO_PROJECT_TEXT = "📋 Без проекту"

rollover_digest_cache: dict[int, list[int]] = {}


class AddTask(StatesGroup):
    text = State()
    label = State()
    category = State()
    project = State()
    date = State()
    date_manual = State()
    time = State()


class EditField(StatesGroup):
    typing = State()


class RecEdit(StatesGroup):
    typing = State()
    interval = State()
    monthday = State()


FREQ_HINT = {"daily": "щодня", "weekdays": "по буднях", "weekly": "щотижня", "monthly": "щомісяця"}
SUGGEST_MIN_COUNT = 3
SUGGEST_MIN_CONFIDENCE = 0.75
DECLINE_COOLDOWN_DAYS = 14
ANALYSIS_INTERVAL_HOURS = 12
HISTORY_DAYS = 60

RC_PROMPTS = {
    "title": "Введіть нову *назву*:",
    "description": "Введіть *опис* (або «-», щоб очистити):",
    "time": "Введіть *час* як *гг:хх*:",
    "start": "Введіть *дату початку* як *дд.мм.рррр*:",
    "end": "Введіть *дату завершення* як *дд.мм.рррр* (або «-», щоб прибрати):",
    "reminder": "За скільки *хвилин* нагадувати? Введіть число (0 — стандартне нагадування):",
}

_bg_tasks: set = set()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


async def _cancel(msg: Message, state: FSMContext, kb=None) -> None:
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb or kb_tasks_menu())


async def _project_title(project_id: str | None) -> str | None:
    if not project_id:
        return None
    p = await projects_db.get_project(project_id)
    return p.get("title") if p else None


async def _fmt_task_full(t: dict) -> str:
    text = fmt_task(t)
    title = await _project_title(t.get("project_id"))
    if title:
        text += f"\n📁 Проєкт: {title}"
    if t.get("recurring_task_id"):
        text += "\n🔁 Повторювана таска"
    if t.get("description"):
        text += f"\n📄 {_md(t['description'])}"
    return text


@router.message(F.text == "📋 Мої задачі")
async def tasks_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("📋 *Мої задачі*\n\nОбери дію:", reply_markup=kb_tasks_menu())


@router.callback_query(F.data == "tasks_menu")
async def tasks_menu_cb(cb: CallbackQuery):
    try:
        await cb.message.edit_text("📋 *Мої задачі*")
    except TelegramAPIError:
        pass
    await cb.message.answer("Обери дію:", reply_markup=kb_tasks_menu())
    await cb.answer()


@router.message(F.text == "🏆 Мій прогрес")
async def progress_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        uid = msg.from_user.id
        udata = await users_db.get_user_state(uid)
        xp = udata.get("xp", 0)
        total_completed = udata.get("total_completed", 0)

        done_tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_DONE])
        recent_completed = sorted(done_tasks, key=lambda t: t.get("completed_at") or "", reverse=True)[:5]

        text = build_progress_text(xp, total_completed, recent_completed)
        await msg.answer(text, reply_markup=kb_main())
    except Exception:
        logger.exception("progress_view failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())


@router.message(F.text == "➕ Додати задачу")
async def new_task_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(AddTask.text)
    await msg.answer("📝 Введіть *текст завдання*:", reply_markup=kb_cancel())


@router.message(StateFilter(AddTask.text))
async def at_text(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    if not msg.text:
        return await msg.answer("⚠️ Введіть текст завдання:", reply_markup=kb_cancel())
    await state.update_data(text=msg.text.strip())
    await state.set_state(AddTask.label)
    await msg.answer("🎨 Оберіть *мітку*:", reply_markup=kb_label())


@router.message(StateFilter(AddTask.label))
async def at_label(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    label = label_from_text(msg.text)
    if not label:
        return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_label())
    await state.update_data(label=label)
    await state.set_state(AddTask.category)
    await msg.answer("🏷 Оберіть *категорію*:", reply_markup=kb_category())


@router.message(StateFilter(AddTask.category))
async def at_category(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    category = category_from_text(msg.text)
    if not category:
        return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_category())
    await state.update_data(category=category)
    projects = await projects_db.get_active_projects(msg.from_user.id)
    project_options = [{"id": str(p["_id"]), "title": p.get("title", "")} for p in projects]
    await state.update_data(available_projects=project_options)
    await state.set_state(AddTask.project)
    await msg.answer("📋 Оберіть *проєкт* (або «Без проекту»):", reply_markup=kb_project_select(project_options))


@router.message(StateFilter(AddTask.project))
async def at_project(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    fd = await state.get_data()
    projects = fd.get("available_projects") or []
    if msg.text == NO_PROJECT_TEXT:
        await state.update_data(project_id=None)
    else:
        project = project_from_text(msg.text, projects)
        if not project:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_project_select(projects))
        await state.update_data(project_id=project["id"])
    await state.set_state(AddTask.date)
    await msg.answer("📅 Коли треба це зробити? Оберіть дату, або обери «без терміну»:", reply_markup=kb_date())


@router.message(StateFilter(AddTask.date))
async def at_date(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    if msg.text == NO_DUE_TEXT:
        return await _save_task(msg, state, due="")
    if msg.text == "✏️ Своя дата (дд.мм.рррр)":
        await state.set_state(AddTask.date_manual)
        return await msg.answer("📅 Введіть дату як *дд.мм.рррр*:", reply_markup=kb_cancel())
    today = datetime.now()
    if msg.text == "📅 Сьогодні":
        date_str = today.strftime("%d.%m.%Y")
    elif msg.text == "📅 Завтра":
        date_str = (today + timedelta(days=1)).strftime("%d.%m.%Y")
    else:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_date())
    await _at_date_save(msg, state, date_str)


@router.message(StateFilter(AddTask.date_manual))
async def at_date_manual(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    try:
        datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return await msg.answer(
            "⚠️ Невірний формат. Введіть дату як *дд.мм.рррр*\n_Наприклад: 10.10.2025_",
            reply_markup=kb_cancel(),
        )
    await _at_date_save(msg, state, raw)


async def _at_date_save(msg: Message, state: FSMContext, date_str: str):
    await state.update_data(date=date_str)
    await state.set_state(AddTask.time)
    await msg.answer(
        f"✅ Дата: *{date_str}*\n\n🕐 Введіть *час* як *гг:хх*:\n_Наприклад: 18:30_",
        reply_markup=kb_cancel(),
    )


@router.message(StateFilter(AddTask.time))
async def at_time(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        return await msg.answer(
            "⚠️ Невірний формат. Введіть час як *гг:хх*\n_Наприклад: 18:30_",
            reply_markup=kb_cancel(),
        )
    fd = await state.get_data()
    due_dt = datetime.strptime(f"{fd['date']} {raw}", "%d.%m.%Y %H:%M")
    await _save_task(msg, state, due=fmt_due(due_dt))


async def _save_task(msg: Message, state: FSMContext, due: str):
    fd = await state.get_data()
    await state.clear()

    try:
        new_id = await tasks_db.next_task_id()
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())

    task = {
        "id": new_id,
        "uid": msg.from_user.id,
        "text": fd["text"],
        "label": fd["label"],
        "category": fd["category"],
        "due": due,
        "status": STATUS_PENDING,
        "pinned": False,
        "subtasks": [],
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "reminded_before": False,
        "missed_flagged": False,
        "missed_counted": False,
        "postponed_count": 0,
        "postponed_today": False,
        "source": "manual",
        "project_id": fd.get("project_id"),
        "estimated_minutes": None,
    }
    try:
        await tasks_db.add_task(task)
        saved = await tasks_db.get_task(new_id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())

    if not saved:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())

    note = "" if due else " (без терміну)"
    await msg.answer(f"✅ *Завдання додано{note}!*\n\n{await _fmt_task_full(saved)}", reply_markup=kb_tasks_menu())
    _spawn_bg(maybe_suggest_recurring(msg.bot, msg.from_user.id, saved))


@router.message(F.text == "📋 Сьогодні")
async def today_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        uid = msg.from_user.id
        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_DONE])
        tasks = [t for t in tasks if is_today(t.get("due", ""))]
        tasks = sort_tasks_by_label_then_due(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 На сьогодні завдань немає.", reply_markup=kb_tasks_menu())
        await msg.answer(f"📋 *Сьогодні* — {len(tasks)} шт.", reply_markup=kb_tasks_menu())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("today_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())


@router.message(F.text == "📅 Майбутні")
async def future_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        uid = msg.from_user.id
        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = [t for t in tasks if not is_today(t.get("due", "")) and not is_missed(t)]
        tasks = sort_tasks(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 Майбутніх завдань немає.", reply_markup=kb_tasks_menu())
        await msg.answer(f"📅 *Майбутні* — {len(tasks)} шт.", reply_markup=kb_tasks_menu())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("future_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())


@router.message(F.text == "✅ Виконані")
async def done_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        uid = msg.from_user.id
        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_DONE])
        tasks = sorted(tasks, key=lambda t: t.get("completed_at") or "", reverse=True)[:60]
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 Ще немає виконаних завдань.", reply_markup=kb_tasks_menu())
        await msg.answer(f"✅ *Виконані* — {len(tasks)} шт. (останні 60)", reply_markup=kb_tasks_menu())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("done_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())


@router.message(F.text == "⭐ Обране")
async def favorites_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        uid = msg.from_user.id
        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = [t for t in tasks if t.get("pinned")]
        tasks = sort_tasks(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("⭐ *Обране*\n\n📭 Немає закріплених завдань.", reply_markup=kb_tasks_menu())
        await msg.answer(f"⭐ *Обране* — {len(tasks)} шт.", reply_markup=kb_tasks_menu())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("favorites_view failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())


@router.message(F.text == "🏷 Категорії")
async def categories_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("🏷 *Оберіть категорію:*", reply_markup=ikb_categories())


@router.callback_query(F.data.startswith("catopen:"))
async def category_open(cb: CallbackQuery):
    try:
        key = cb.data.split(":")[1]
        cat = CATEGORIES.get(key)
        if not cat:
            return await cb.answer("Невідома категорія", show_alert=True)
        uid = cb.from_user.id
        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = [t for t in tasks if t.get("category") == key]
        tasks = sort_tasks_by_label_then_due(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            await cb.message.edit_text(f"{cat['emoji']} *{cat['name']}*\n\n📭 Немає завдань у цій категорії.")
        else:
            await cb.message.edit_text(f"{cat['emoji']} *{cat['name']}* — {len(tasks)} шт.", reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("category_open failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("page:"))
async def page_tasks(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        page = int(cb.data.split(":")[1])
        tasks = user_list_cache.get(uid) or []
        await cb.message.edit_reply_markup(reply_markup=ikb_tasks_list(tasks, page))
        await cb.answer()
    except Exception:
        logger.exception("page_tasks failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "back_to_list")
async def back_to_list(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        tasks = user_list_cache.get(uid) or []
        if not tasks:
            await cb.message.edit_text("📭 Список порожній.")
            return await cb.answer()
        await cb.message.edit_text("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("back_to_list failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("view:"))
async def view_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        await cb.message.edit_text(await _fmt_task_full(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer()
    except Exception:
        logger.exception("view_task failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("viewday:"))
async def view_day_cb(cb: CallbackQuery):
    try:
        date_str = cb.data.split(":", 1)[1]
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        uid = cb.from_user.id

        tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_DONE])
        tasks = [t for t in tasks if (parse_due(t.get("due", "")) or datetime.min).date() == target]
        tasks = sort_tasks_by_label_then_due(tasks)
        user_list_cache[uid] = tasks

        pretty = target.strftime("%d.%m.%Y")
        if not tasks:
            await cb.message.edit_text(f"📭 На {pretty} задач немає.")
        else:
            await cb.message.edit_text(f"📅 *{pretty}* — {len(tasks)} шт.", reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("view_day_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("done:"))
async def task_done(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)

        state = await users_db.get_user_state(t["uid"])
        old_xp = state.get("xp", 0)
        gain = LABEL_XP.get(t.get("label", "idea"), 10)
        new_xp = old_xp + gain
        old_level, _, _ = level_progress(old_xp)
        new_level, _, _ = level_progress(new_xp)

        await tasks_db.update_task(tid, {"status": STATUS_DONE, "completed_at": datetime.now().isoformat()})
        await users_db.save_user_state(t["uid"], {
            "xp": new_xp,
            "total_completed": state.get("total_completed", 0) + 1,
        })
        t = await tasks_db.get_task(tid)

        extra = f"\n\n✨ +{gain} XP"

        try:
            await cb.message.edit_text(f"✅ *Виконано!*\n\n{await _fmt_task_full(t)}{extra}")
        except TelegramAPIError:
            await cb.message.answer(f"✅ *Виконано!*\n\n{await _fmt_task_full(t)}{extra}")
        await cb.answer("✅ Виконано!")

        if new_level > old_level:
            try:
                await cb.message.answer(f"🎉 *Новий рівень!*\nТи досяг {new_level} рівня 🏆")
            except TelegramAPIError:
                logger.exception("Не вдалося надіслати повідомлення про новий рівень для uid=%s", t["uid"])
    except Exception:
        logger.exception("task_done failed")
        await _safe_alert(cb)


async def _postpone(cb: CallbackQuery, tid: int, new_due: datetime):
    t = await tasks_db.get_task(tid)
    if not t:
        return await cb.answer("Не знайдено!", show_alert=True)
    await tasks_db.update_task(tid, {
        "due": fmt_due(new_due),
        "status": STATUS_PENDING,
        "reminded_before": False,
        "missed_flagged": False,
        "postponed_count": t.get("postponed_count", 0) + 1,
        "postponed_today": True,
    })
    state = await users_db.get_user_state(t["uid"])
    await users_db.save_user_state(t["uid"], {"total_postponed": state.get("total_postponed", 0) + 1})
    t = await tasks_db.get_task(tid)
    text = f"🔁 *Перенесено!*\n\n{await _fmt_task_full(t)}"
    try:
        await cb.message.edit_text(text, reply_markup=ikb_task_actions(tid, t))
    except TelegramAPIError:
        await cb.message.answer(text, reply_markup=ikb_task_actions(tid, t))
    await cb.answer("🔁 Перенесено")


@router.callback_query(F.data.startswith("postp1h:"))
async def postpone_1h(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await _postpone(cb, tid, datetime.now() + timedelta(hours=1))
    except Exception:
        logger.exception("postpone_1h failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("postptom:"))
async def postpone_tomorrow(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        old_due = parse_due(t.get("due", "")) or datetime.now()
        new_due = (datetime.now() + timedelta(days=1)).replace(
            hour=old_due.hour, minute=old_due.minute, second=0, microsecond=0
        )
        await _postpone(cb, tid, new_due)
    except Exception:
        logger.exception("postpone_tomorrow failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("autoresched:"))
async def auto_reschedule_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":", 1)[1])
        uid = cb.from_user.id
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)

        new_day = await reschedule_service.reschedule_task_to_next_free_day(uid, tid, auto=False)
        if not new_day:
            return await cb.answer("Не знайдено!", show_alert=True)

        t = await tasks_db.get_task(tid)
        pretty = new_day.strftime("%d.%m.%Y")
        text = f"✅ *Задачу перенесено*\n\n📝 {t.get('text','')}\n📅 Новий день: {pretty}"
        try:
            await cb.message.edit_text(text, reply_markup=ikb_view_day(new_day))
        except TelegramAPIError:
            await cb.message.answer(text, reply_markup=ikb_view_day(new_day))
        await cb.answer("🔁 Перенесено")
    except Exception:
        logger.exception("auto_reschedule_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("dismiss_rollover:"))
async def dismiss_rollover_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":", 1)[1])
        await tasks_db.update_task(tid, {"reschedule_prompt_sent_at": datetime.now().strftime("%Y-%m-%d")})
        try:
            await cb.message.edit_text("🔕 Залишено як є. Нагадаю знову, якщо не виконаєш.")
        except TelegramAPIError:
            pass
        await cb.answer()
    except Exception:
        logger.exception("dismiss_rollover_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "resched_all")
async def resched_all_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        task_ids = rollover_digest_cache.get(uid) or []
        if not task_ids:
            return await cb.answer("Список застарів, спробуй ще раз завтра.", show_alert=True)

        results = await reschedule_service.reschedule_many(uid, task_ids)
        rollover_digest_cache.pop(uid, None)

        if not results:
            return await cb.answer("Нічого не перенесено (задачі вже змінились).", show_alert=True)

        lines = ["✅ *Перенесено:*", ""]
        for tid, day in results.items():
            t = await tasks_db.get_task(tid)
            title = (t or {}).get("text", "")
            lines.append(f"📝 {title} → {day.strftime('%d.%m.%Y')}")
        try:
            await cb.message.edit_text("\n".join(lines))
        except TelegramAPIError:
            await cb.message.answer("\n".join(lines))
        await cb.answer(f"Перенесено {len(results)} задач")
    except Exception:
        logger.exception("resched_all_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "resched_pick")
async def resched_pick_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        task_ids = rollover_digest_cache.get(uid) or []
        if not task_ids:
            return await cb.answer("Список застарів, спробуй ще раз завтра.", show_alert=True)

        tasks = []
        for tid in task_ids:
            t = await tasks_db.get_task(tid)
            if t:
                tasks.append(t)
        if not tasks:
            return await cb.answer("Список застарів.", show_alert=True)

        user_list_cache[uid] = tasks
        try:
            await cb.message.edit_text(
                "Обери задачу — відкриється картка, де можна перенести саме її:",
                reply_markup=ikb_tasks_list(tasks),
            )
        except TelegramAPIError:
            await cb.message.answer(
                "Обери задачу — відкриється картка, де можна перенести саме її:",
                reply_markup=ikb_tasks_list(tasks),
            )
        await cb.answer()
    except Exception:
        logger.exception("resched_pick_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "dismiss_all")
async def dismiss_all_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        task_ids = rollover_digest_cache.pop(uid, None) or []
        today_str = datetime.now().strftime("%Y-%m-%d")
        for tid in task_ids:
            await tasks_db.update_task(tid, {"reschedule_prompt_sent_at": today_str})
        try:
            await cb.message.edit_text("🔕 Залишено як є. Нагадаю знову, якщо щось лишиться невиконаним.")
        except TelegramAPIError:
            pass
        await cb.answer()
    except Exception:
        logger.exception("dismiss_all_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("deltask:"))
async def delete_task_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        await tasks_db.delete_task(tid)
        await cb.message.edit_text("🗑 Завдання видалено.")
        await cb.answer("Видалено!")
    except Exception:
        logger.exception("delete_task_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("pin:"))
async def pin_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await tasks_db.update_task(tid, {"pinned": True})
        t = await tasks_db.get_task(tid)
        await cb.message.edit_text(await _fmt_task_full(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer("⭐ Закріплено!")
    except Exception:
        logger.exception("pin_task failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("unpin:"))
async def unpin_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await tasks_db.update_task(tid, {"pinned": False})
        t = await tasks_db.get_task(tid)
        await cb.message.edit_text(await _fmt_task_full(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer("📌 Відкріплено")
    except Exception:
        logger.exception("unpin_task failed")
        await _safe_alert(cb)


def _render_subtasks(tid: int, t: dict):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    subtasks = t.get("subtasks") or []
    if not subtasks:
        text = f"📝 *Підзадачі* — №{tid}\n\nПоки що немає підзадач.\nДодай їх через ✏️ Редагувати → ➕ Додати підзадачі."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")]])
        return text, kb
    rows = []
    for s in subtasks:
        box = "☑" if s.get("done") else "☐"
        rows.append([InlineKeyboardButton(text=f"{box} {s['text'][:40]}", callback_data=f"subtoggle:{tid}:{s['id']}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    done_n = sum(1 for s in subtasks if s.get("done"))
    text = f"📝 *Підзадачі* — №{tid}\n\n{done_n}/{len(subtasks)} виконано"
    return text, kb


@router.callback_query(F.data.startswith("subtasks:"))
async def subtasks_view(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        text, kb = _render_subtasks(tid, t)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("subtasks_view failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("subtoggle:"))
async def subtask_toggle(cb: CallbackQuery):
    try:
        _, tid_s, subid = cb.data.split(":")
        tid = int(tid_s)
        t = await tasks_db.get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        subtasks = t.get("subtasks") or []
        for s in subtasks:
            if s["id"] == subid:
                s["done"] = not s.get("done")
        await tasks_db.update_task(tid, {"subtasks": subtasks})
        t = await tasks_db.get_task(tid)
        text, kb = _render_subtasks(tid, t)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("subtask_toggle failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("edit:"))
async def edit_task_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await cb.message.edit_text(f"✏️ *Редагування №{tid}*\nОберіть поле:", reply_markup=ikb_edit_fields(tid))
        await cb.answer()
    except Exception:
        logger.exception("edit_task_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("editfield:"))
async def edit_field_choose(cb: CallbackQuery, state: FSMContext):
    try:
        _, tid_s, field = cb.data.split(":", 2)
        tid = int(tid_s)
        labels = {
            "text": "Текст завдання",
            "label": "Мітка",
            "category": "Категорія",
            "date": "Дата (дд.мм.рррр)",
            "time": "Час (гг:хх)",
            "subtasks_add": "Нові підзадачі (кожна з нового рядка)",
        }
        await state.set_state(EditField.typing)
        await state.update_data(edit_tid=tid, edit_field=field)
        if field == "label":
            kb = kb_label()
        elif field == "category":
            kb = kb_category()
        else:
            kb = kb_cancel()
        await cb.message.answer(f"Введіть нове значення для *{labels.get(field, field)}*:", reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("edit_field_choose failed")
        await _safe_alert(cb)


@router.message(StateFilter(EditField.typing))
async def edit_field_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    fd = await state.get_data()
    tid, field = fd["edit_tid"], fd["edit_field"]
    t = await tasks_db.get_task(tid)
    if not t:
        await state.clear()
        return await msg.answer("Завдання не знайдено.", reply_markup=kb_tasks_menu())

    if field == "text":
        await tasks_db.update_task(tid, {"text": msg.text.strip()})
    elif field == "label":
        label = label_from_text(msg.text)
        if not label:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_label())
        await tasks_db.update_task(tid, {"label": label})
    elif field == "category":
        category = category_from_text(msg.text)
        if not category:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_category())
        await tasks_db.update_task(tid, {"category": category})
    elif field == "date":
        if msg.text.strip() == NO_DUE_TEXT:
            await tasks_db.update_task(tid, {"due": "", "status": STATUS_PENDING,
                                              "reminded_before": False, "missed_flagged": False})
        else:
            try:
                datetime.strptime(msg.text.strip(), "%d.%m.%Y")
            except ValueError:
                return await msg.answer(
                    "⚠️ Невірний формат. Введіть як *дд.мм.рррр*, або напишіть "
                    f"«{NO_DUE_TEXT}», щоб прибрати термін:",
                    reply_markup=kb_cancel(),
                )
            old_due = parse_due(t.get("due", ""))
            time_part = old_due.strftime("%H:%M") if old_due else "00:00"
            new_due = f"{msg.text.strip()} {time_part}"
            await tasks_db.update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                              "reminded_before": False, "missed_flagged": False})
    elif field == "time":
        try:
            datetime.strptime(msg.text.strip(), "%H:%M")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *гг:хх*:", reply_markup=kb_cancel())
        old_due = parse_due(t.get("due", ""))
        date_part = old_due.strftime("%d.%m.%Y") if old_due else datetime.now().strftime("%d.%m.%Y")
        new_due = f"{date_part} {msg.text.strip()}"
        await tasks_db.update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                          "reminded_before": False, "missed_flagged": False})
    elif field == "subtasks_add":
        subtasks = t.get("subtasks") or []
        added = 0
        for line in msg.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            subtasks.append({"id": secrets.token_hex(2), "text": line, "done": False})
            added += 1
        await tasks_db.update_task(tid, {"subtasks": subtasks})
        if added == 0:
            return await msg.answer("⚠️ Не знайшов жодного рядка з текстом. Спробуй ще раз:", reply_markup=kb_cancel())

    await state.clear()
    t = await tasks_db.get_task(tid)
    if t:
        await msg.answer(f"✅ Оновлено!\n\n{await _fmt_task_full(t)}", reply_markup=kb_tasks_menu())
    else:
        await msg.answer("Завдання не знайдено.", reply_markup=kb_tasks_menu())


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass


def _md(text: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", text or "")


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _pretty_date(iso: str | None) -> str:
    if not iso:
        return "—"
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def _rc_status(rec: dict) -> str:
    if rec.get("paused"):
        return "⏸ На паузі"
    if not rec.get("active"):
        return "⚪ Завершена"
    return "🟢 Активна"


def _fmt_recurring(rec: dict) -> str:
    cat = CATEGORIES.get(rec.get("category"), {})
    lab = LABELS.get(rec.get("priority"), {})
    lines = [f"🔁 *{_md(rec.get('title', ''))}*"]
    if rec.get("description"):
        lines.append(_md(rec["description"]))
    lines += [
        "",
        describe_schedule(rec["schedule"]),
        f"📅 Наступне: {_pretty_date(rec.get('next_date'))}",
        f"{cat.get('emoji', '')} {cat.get('name', '')}  {lab.get('emoji', '')} {lab.get('name', '')}",
        f"▶️ Початок: {_pretty_date(rec.get('start_date'))}",
    ]
    if rec.get("end_date"):
        lines.append(f"🏁 Завершення: {_pretty_date(rec['end_date'])}")
    rm = rec.get("reminder_minutes")
    lines.append(f"⏰ Нагадування: за {rm} хв" if rm else "⏰ Нагадування: стандартне")
    lines.append(_rc_status(rec))
    return "\n".join(lines)


async def _rc_edit(cb: CallbackQuery, text: str, kb=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await cb.message.answer(text, reply_markup=kb)


async def _own_rec(cb: CallbackQuery, rid: str) -> dict | None:
    rec = await tasks_db.get_recurring(rid)
    if not rec or rec.get("uid") != cb.from_user.id:
        await cb.answer("Не знайдено!", show_alert=True)
        return None
    return rec


async def _recurring_list_view(uid: int):
    recs = await tasks_db.list_recurring(uid)
    if not recs:
        text = (
            "🔁 *Повторювані таски*\n\nПоки що немає. Бот запропонує, коли помітить регулярні дії, "
            "або натисни «🔁 Зробити повторюваною» на картці задачі."
        )
        return text, ikb_recurring_list([])
    blocks = ["🔁 *Повторювані таски*", ""]
    for r in recs:
        blocks.append(f"🔁 {_md(r.get('title', ''))}\n{describe_schedule(r['schedule'])}\n{_rc_status(r)}\n")
    return "\n".join(blocks), ikb_recurring_list(recs)


@router.message(F.text == "🔁 Повторювані таски")
async def recurring_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        text, kb = await _recurring_list_view(msg.from_user.id)
        await msg.answer(text, reply_markup=kb)
    except Exception:
        logger.exception("recurring_menu failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())


@router.callback_query(F.data == "rc_list")
async def rc_list_cb(cb: CallbackQuery):
    try:
        text, kb = await _recurring_list_view(cb.from_user.id)
        await _rc_edit(cb, text, kb)
        await cb.answer()
    except Exception:
        logger.exception("rc_list_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcv:"))
async def rc_view(cb: CallbackQuery):
    try:
        rec = await _own_rec(cb, cb.data.split(":", 1)[1])
        if not rec:
            return
        await _rc_edit(cb, _fmt_recurring(rec), ikb_recurring_card(rec))
        await cb.answer()
    except Exception:
        logger.exception("rc_view failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcp:"))
async def rc_pause(cb: CallbackQuery):
    try:
        rid = cb.data.split(":", 1)[1]
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        if rec.get("paused"):
            await tasks_db.update_recurring(rid, {"paused": False})
            rec = await tasks_db.replan_recurring(rid)
            note = "▶️ Повторення відновлено"
        else:
            await tasks_db.update_recurring(rid, {"paused": True})
            rec = await tasks_db.get_recurring(rid)
            note = "⏸ Повторення призупинено"
        await _rc_edit(cb, f"{note}\n\n{_fmt_recurring(rec)}", ikb_recurring_card(rec))
        await cb.answer(note)
    except Exception:
        logger.exception("rc_pause failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcn:"))
async def rc_skip_next(cb: CallbackQuery):
    try:
        rid = cb.data.split(":", 1)[1]
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        res = await tasks_db.skip_next_recurring(rid)
        if not res:
            return await cb.answer("Немає наступного виконання.", show_alert=True)
        skipped, _ = res
        rec = await tasks_db.get_recurring(rid)
        await _rc_edit(
            cb,
            f"❌ Пропущено виконання {skipped.strftime('%d.%m.%Y')}\n\n{_fmt_recurring(rec)}",
            ikb_recurring_card(rec),
        )
        await cb.answer("Пропущено")
    except Exception:
        logger.exception("rc_skip_next failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcd:"))
async def rc_delete_ask(cb: CallbackQuery):
    try:
        rid = cb.data.split(":", 1)[1]
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        await _rc_edit(
            cb,
            f"🗑 Видалити повторення *{_md(rec.get('title', ''))}*?\n\nВже створені таски залишаться як звичайні.",
            ikb_rc_confirm_delete(rid),
        )
        await cb.answer()
    except Exception:
        logger.exception("rc_delete_ask failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcdy:"))
async def rc_delete_confirm(cb: CallbackQuery):
    try:
        rid = cb.data.split(":", 1)[1]
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        await tasks_db.delete_recurring(rid)
        await _rc_edit(cb, "🗑 Повторення видалено. Вже створені таски залишились як звичайні.")
        await cb.answer("Видалено")
    except Exception:
        logger.exception("rc_delete_confirm failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rce:"))
async def rc_edit_menu(cb: CallbackQuery):
    try:
        rid = cb.data.split(":", 1)[1]
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        await _rc_edit(cb, f"✏️ *Редагування*\n\n{_fmt_recurring(rec)}", ikb_recurring_edit(rid))
        await cb.answer()
    except Exception:
        logger.exception("rc_edit_menu failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcf:"))
async def rc_field(cb: CallbackQuery, state: FSMContext):
    try:
        _, rid, field = cb.data.split(":", 2)
        rec = await _own_rec(cb, rid)
        if not rec:
            return
        if field == "category":
            await _rc_edit(cb, "🏷 Оберіть категорію:", ikb_rc_category(rid))
        elif field == "priority":
            await _rc_edit(cb, "🎨 Оберіть пріоритет:", ikb_rc_priority(rid))
        elif field in RC_PROMPTS:
            await state.set_state(RecEdit.typing)
            await state.update_data(rc_rid=rid, rc_field=field)
            await cb.message.answer(RC_PROMPTS[field], reply_markup=kb_cancel())
        await cb.answer()
    except Exception:
        logger.exception("rc_field failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcfc:"))
async def rc_set_category(cb: CallbackQuery):
    try:
        _, rid, key = cb.data.split(":", 2)
        if not await _own_rec(cb, rid):
            return
        if key not in CATEGORIES:
            return await cb.answer("Невідома категорія", show_alert=True)
        await tasks_db.update_recurring(rid, {"category": key})
        rec = await tasks_db.get_recurring(rid)
        await _rc_edit(cb, f"✅ Оновлено\n\n{_fmt_recurring(rec)}", ikb_recurring_card(rec))
        await cb.answer()
    except Exception:
        logger.exception("rc_set_category failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcfl:"))
async def rc_set_priority(cb: CallbackQuery):
    try:
        _, rid, key = cb.data.split(":", 2)
        if not await _own_rec(cb, rid):
            return
        if key not in LABELS:
            return await cb.answer("Невідомий пріоритет", show_alert=True)
        await tasks_db.update_recurring(rid, {"priority": key})
        rec = await tasks_db.get_recurring(rid)
        await _rc_edit(cb, f"✅ Оновлено\n\n{_fmt_recurring(rec)}", ikb_recurring_card(rec))
        await cb.answer()
    except Exception:
        logger.exception("rc_set_priority failed")
        await _safe_alert(cb)


@router.message(StateFilter(RecEdit.typing))
async def rc_edit_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    fd = await state.get_data()
    rid, field = fd.get("rc_rid"), fd.get("rc_field")
    rec = await tasks_db.get_recurring(rid) if rid else None
    if not rec or rec.get("uid") != msg.from_user.id:
        await state.clear()
        return await msg.answer("Не знайдено.", reply_markup=kb_tasks_menu())

    raw = (msg.text or "").strip()
    fields: dict = {}
    replan = False

    if field == "title":
        if not raw:
            return await msg.answer("⚠️ Введіть назву:", reply_markup=kb_cancel())
        fields["title"] = raw[:200]
    elif field == "description":
        fields["description"] = "" if raw == "-" else raw[:1000]
    elif field == "time":
        try:
            hhmm = datetime.strptime(raw, "%H:%M").strftime("%H:%M")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *гг:хх*:", reply_markup=kb_cancel())
        schedule = dict(rec["schedule"])
        schedule["time"] = hhmm
        fields["schedule"] = schedule
        replan = True
    elif field == "start":
        try:
            fields["start_date"] = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *дд.мм.рррр*:", reply_markup=kb_cancel())
        replan = True
    elif field == "end":
        if raw == "-":
            fields["end_date"] = None
        else:
            try:
                fields["end_date"] = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
            except ValueError:
                return await msg.answer(
                    "⚠️ Невірний формат. Введіть як *дд.мм.рррр* або «-»:", reply_markup=kb_cancel()
                )
        replan = True
    elif field == "reminder":
        if not raw.isdigit() or int(raw) > 1440:
            return await msg.answer("⚠️ Введіть число хвилин від 0 до 1440:", reply_markup=kb_cancel())
        fields["reminder_minutes"] = int(raw) or None

    await tasks_db.update_recurring(rid, fields)
    rec = await tasks_db.replan_recurring(rid) if replan else await tasks_db.get_recurring(rid)
    await state.clear()
    await msg.answer("✅ Оновлено!", reply_markup=kb_tasks_menu())
    await msg.answer(_fmt_recurring(rec), reply_markup=ikb_recurring_card(rec))


async def _apply_schedule(uid: int, ctx: str, kind: str, days=None, interval: int = 1, day: int | None = None):
    kind_ctx, ref = ctx[0], ctx[1:]
    sug = rec = task = None
    tz = DEFAULT_TZ
    if kind_ctx == "s":
        sug = await tasks_db.get_suggestion(ref)
        if not sug or sug.get("uid") != uid or sug.get("status") == "accepted":
            return None
        hhmm = sug.get("time") or "18:00"
    elif kind_ctx == "r":
        rec = await tasks_db.get_recurring(ref)
        if not rec or rec.get("uid") != uid:
            return None
        hhmm = rec["schedule"]["time"]
        tz = rec["schedule"].get("tz") or DEFAULT_TZ
    else:
        task = await tasks_db.get_task(int(ref))
        if not task or task.get("uid") != uid:
            return None
        due = parse_due(task.get("due", ""))
        hhmm = due.strftime("%H:%M") if due else "18:00"

    schedule = build_schedule(kind, hhmm, tz, days=days, interval=interval, day=day)

    if kind_ctx == "r":
        await tasks_db.update_recurring(ref, {"schedule": schedule})
        return await tasks_db.replan_recurring(ref)

    start = now_local(tz).date() + timedelta(days=1)
    if task:
        due = parse_due(task.get("due", ""))
        if due:
            start = max(start, due.date() + timedelta(days=1))

    if kind_ctx == "s":
        created = await tasks_db.create_recurring(
            uid, sug["title"], sug.get("category") or "other", sug.get("label") or "medium",
            schedule, ai_confidence=sug.get("confidence"), start_date=start,
        )
        await tasks_db.update_suggestion(ref, {"status": "accepted", "recurring_task_id": str(created["_id"])})
        return created

    return await tasks_db.create_recurring(
        uid, task["text"], task.get("category") or "other", task.get("label") or "medium",
        schedule, start_date=start,
    )


async def _after_schedule(cb: CallbackQuery, ctx: str, rec: dict | None) -> None:
    if not rec:
        return await cb.answer("Вже оброблено або не знайдено.", show_alert=True)
    header = "✅ Графік збережено" if ctx[0] == "r" else "✅ Повторювану таску створено"
    await _rc_edit(cb, f"{header}\n\n{_fmt_recurring(rec)}", ikb_recurring_card(rec))
    await cb.answer()


async def _after_schedule_msg(msg: Message, ctx: str | None, rec: dict | None) -> None:
    if not rec:
        return await msg.answer("Не знайдено.", reply_markup=kb_tasks_menu())
    header = "✅ Графік збережено" if ctx and ctx[0] == "r" else "✅ Повторювану таску створено"
    await msg.answer(header, reply_markup=kb_tasks_menu())
    await msg.answer(_fmt_recurring(rec), reply_markup=ikb_recurring_card(rec))


@router.callback_query(F.data.startswith("rcsch_open:"))
async def rc_sched_open(cb: CallbackQuery):
    try:
        ctx = cb.data.split(":", 1)[1]
        await _rc_edit(cb, "📅 *Оберіть графік повторення:*", ikb_rc_schedule(ctx))
        await cb.answer()
    except Exception:
        logger.exception("rc_sched_open failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcsch_x:"))
async def rc_sched_cancel(cb: CallbackQuery):
    try:
        ctx = cb.data.split(":", 1)[1]
        kind_ctx, ref = ctx[0], ctx[1:]
        if kind_ctx == "r":
            rec = await _own_rec(cb, ref)
            if not rec:
                return
            await _rc_edit(cb, _fmt_recurring(rec), ikb_recurring_card(rec))
        elif kind_ctx == "t":
            t = await tasks_db.get_task(int(ref))
            if not t:
                return await cb.answer("Не знайдено!", show_alert=True)
            await _rc_edit(cb, await _fmt_task_full(t), ikb_task_actions(t["id"], t))
        else:
            await _rc_edit(cb, "Ок, нічого не змінено.")
        await cb.answer()
    except Exception:
        logger.exception("rc_sched_cancel failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcsch:"))
async def rc_sched_pick(cb: CallbackQuery, state: FSMContext):
    try:
        _, ctx, kind = cb.data.split(":", 2)
        if kind in ("weekly", "biweekly", "days"):
            await state.update_data(rc_ctx=ctx, rc_kind=kind, rc_days=[])
            await _rc_edit(cb, "📅 Оберіть дні тижня:", ikb_rc_days([]))
            return await cb.answer()
        if kind == "every_n":
            await state.set_state(RecEdit.interval)
            await state.update_data(rc_ctx=ctx)
            await cb.message.answer("Кожні скільки днів? Введіть число:", reply_markup=kb_cancel())
            return await cb.answer()
        if kind == "monthday":
            await state.set_state(RecEdit.monthday)
            await state.update_data(rc_ctx=ctx)
            await cb.message.answer("Якого числа місяця? Введіть число від 1 до 31:", reply_markup=kb_cancel())
            return await cb.answer()
        rec = await _apply_schedule(cb.from_user.id, ctx, kind)
        await _after_schedule(cb, ctx, rec)
    except Exception:
        logger.exception("rc_sched_pick failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcdw:"))
async def rc_days_toggle(cb: CallbackQuery, state: FSMContext):
    try:
        fd = await state.get_data()
        if not fd.get("rc_ctx"):
            return await cb.answer("Сесія застаріла, почни знову.", show_alert=True)
        n = int(cb.data.split(":")[1])
        days = set(fd.get("rc_days") or [])
        days ^= {n}
        await state.update_data(rc_days=sorted(days))
        await cb.message.edit_reply_markup(reply_markup=ikb_rc_days(days))
        await cb.answer()
    except Exception:
        logger.exception("rc_days_toggle failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "rcdw_ok")
async def rc_days_done(cb: CallbackQuery, state: FSMContext):
    try:
        fd = await state.get_data()
        ctx, kind, days = fd.get("rc_ctx"), fd.get("rc_kind"), fd.get("rc_days") or []
        if not ctx or not kind:
            return await cb.answer("Сесія застаріла, почни знову.", show_alert=True)
        if not days:
            return await cb.answer("Оберіть хоча б один день.", show_alert=True)
        await state.clear()
        rec = await _apply_schedule(cb.from_user.id, ctx, kind, days=days)
        await _after_schedule(cb, ctx, rec)
    except Exception:
        logger.exception("rc_days_done failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "rcdw_cancel")
async def rc_days_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _rc_edit(cb, "Скасовано.")
    await cb.answer()


@router.message(StateFilter(RecEdit.interval))
async def rc_interval_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        return await msg.answer("⚠️ Введіть число від 1 до 365:", reply_markup=kb_cancel())
    ctx = (await state.get_data()).get("rc_ctx")
    await state.clear()
    rec = await _apply_schedule(msg.from_user.id, ctx, "every_n", interval=int(raw)) if ctx else None
    await _after_schedule_msg(msg, ctx, rec)


@router.message(StateFilter(RecEdit.monthday))
async def rc_monthday_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 31:
        return await msg.answer("⚠️ Введіть число від 1 до 31:", reply_markup=kb_cancel())
    ctx = (await state.get_data()).get("rc_ctx")
    await state.clear()
    rec = await _apply_schedule(msg.from_user.id, ctx, "monthday", day=int(raw)) if ctx else None
    await _after_schedule_msg(msg, ctx, rec)


@router.callback_query(F.data.startswith("rcsy:"))
async def rc_sugg_yes(cb: CallbackQuery):
    try:
        _, sid, kind = cb.data.split(":", 2)
        ctx = "s" + sid
        rec = await _apply_schedule(cb.from_user.id, ctx, kind)
        await _after_schedule(cb, ctx, rec)
    except Exception:
        logger.exception("rc_sugg_yes failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcsp:"))
async def rc_sugg_pick(cb: CallbackQuery):
    try:
        sid = cb.data.split(":", 1)[1]
        sug = await tasks_db.get_suggestion(sid)
        if not sug or sug.get("uid") != cb.from_user.id or sug.get("status") == "accepted":
            return await cb.answer("Вже оброблено або не знайдено.", show_alert=True)
        await _rc_edit(cb, "📅 *Оберіть графік повторення:*", ikb_rc_schedule("s" + sid))
        await cb.answer()
    except Exception:
        logger.exception("rc_sugg_pick failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcsn:"))
async def rc_sugg_no(cb: CallbackQuery):
    try:
        sid = cb.data.split(":", 1)[1]
        sug = await tasks_db.get_suggestion(sid)
        if not sug or sug.get("uid") != cb.from_user.id:
            return await cb.answer("Не знайдено!", show_alert=True)
        await tasks_db.update_suggestion(sid, {"status": "declined"})
        await _rc_edit(cb, "Ок, не створюю.")
        await cb.answer()
    except Exception:
        logger.exception("rc_sugg_no failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcsx:"))
async def rc_sugg_block(cb: CallbackQuery):
    try:
        sid = cb.data.split(":", 1)[1]
        sug = await tasks_db.get_suggestion(sid)
        if not sug or sug.get("uid") != cb.from_user.id:
            return await cb.answer("Не знайдено!", show_alert=True)
        await tasks_db.update_suggestion(sid, {"status": "blocked"})
        await _rc_edit(cb, "🔕 Більше не пропонуватиму це.")
        await cb.answer()
    except Exception:
        logger.exception("rc_sugg_block failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcskip:"))
async def rc_skip_occurrence(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":", 1)[1])
        t = await tasks_db.get_task(tid)
        if not t or t.get("uid") != cb.from_user.id:
            return await cb.answer("Не знайдено!", show_alert=True)
        rid = t.get("recurring_task_id")
        await tasks_db.delete_task(tid)
        rec = await tasks_db.get_recurring(rid) if rid else None
        text = "❌ Це виконання пропущено."
        if rec and rec.get("next_date"):
            text += f"\n📅 Наступне: {_pretty_date(rec['next_date'])}"
        await _rc_edit(cb, text)
        await cb.answer("Пропущено")
    except Exception:
        logger.exception("rc_skip_occurrence failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcmv:"))
async def rc_move_menu(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":", 1)[1])
        await cb.message.edit_reply_markup(reply_markup=ikb_recurring_move(tid))
        await cb.answer()
    except Exception:
        logger.exception("rc_move_menu failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("rcback:"))
async def rc_move_back(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":", 1)[1])
        await cb.message.edit_reply_markup(reply_markup=ikb_recurring_notice(tid))
        await cb.answer()
    except Exception:
        logger.exception("rc_move_back failed")
        await _safe_alert(cb)


def _tokens(text: str) -> set:
    return {w[:5] for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2 and not w.isdigit()}


def _is_similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and ta & tb:
        return True
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio() >= 0.6


def _norm_key(text: str) -> str:
    return " ".join(sorted(_tokens(text)))


def _recent(t: dict, days: int) -> bool:
    try:
        return datetime.now() - datetime.fromisoformat(t.get("created_at") or "") <= timedelta(days=days)
    except ValueError:
        return True


def _already_handled(key: str, recs: list, sugs: list) -> bool:
    for r in recs:
        if difflib.SequenceMatcher(None, _norm_key(r.get("title", "")), key).ratio() >= 0.8:
            return True
    now = datetime.now()
    for s in sugs:
        if difflib.SequenceMatcher(None, s.get("key", ""), key).ratio() < 0.8:
            continue
        status = s.get("status")
        if status in ("pending", "accepted", "blocked"):
            return True
        if status == "declined":
            try:
                if now - datetime.fromisoformat(s.get("updated_at", "")) < timedelta(days=DECLINE_COOLDOWN_DAYS):
                    return True
            except ValueError:
                return True
    return False


async def _ai_groups(items: list[dict]) -> list[dict]:
    lines = "\n".join(f"{i['id']}: {str(i['text'])[:120]}" for i in items)
    prompt = (
        "Ти аналізуєш історію задач користувача. Знайди групи задач, які описують ОДНУ Й ТУ САМУ "
        "регулярну дію за змістом, навіть якщо формулювання, числа, відмінки чи сленг різні "
        "(наприклад «Додати товар в Instagram», «Викласти новий товар в інсту»). "
        "Різні за змістом дії не об'єднуй. Одноразові, випадкові та унікальні задачі не включай. "
        "Кожна група має містити щонайменше 3 задачі.\n\n"
        f"Задачі (id: текст):\n{lines}\n\n"
        "Поверни ЛИШЕ JSON без пояснень:\n"
        '{"groups": [{"task_ids": [числа], "is_recurring_candidate": true, "confidence": 0.0, '
        '"normalized_title": "коротка узагальнена назва українською", '
        '"suggested_frequency": "daily або weekdays або weekly або monthly"}]}'
    )
    data = await ai_service.generate_json(prompt, temperature=0.2)
    groups = (data or {}).get("groups")
    return groups if isinstance(groups, list) else []


async def maybe_suggest_recurring(bot, uid: int, task: dict) -> None:
    try:
        if not ai_service.is_available():
            return
        state = await users_db.get_user_state(uid)
        last = state.get("recurring_last_check")
        if last:
            try:
                if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=ANALYSIS_INTERVAL_HOURS):
                    return
            except ValueError:
                pass

        all_tasks = await tasks_db.get_user_tasks(uid)
        history = [
            t for t in all_tasks
            if t.get("id") != task["id"] and not t.get("recurring_task_id")
            and t.get("text") and _recent(t, HISTORY_DAYS)
        ]
        similar = [t for t in history if _is_similar(task["text"], t["text"])]
        if len(similar) < SUGGEST_MIN_COUNT - 1:
            return

        await users_db.save_user_state(uid, {"recurring_last_check": datetime.now().isoformat()})

        history.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        items = [task] + history[:60]
        groups = await _ai_groups([{"id": t["id"], "text": t["text"]} for t in items])
        if not groups:
            return

        by_id = {t["id"]: t for t in items}
        recs = await tasks_db.list_recurring(uid)
        sugs = await tasks_db.get_suggestions(uid)

        for g in groups:
            if not isinstance(g, dict) or g.get("is_recurring_candidate") is not True:
                continue
            try:
                conf = float(g.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            ids = set()
            for i in g.get("task_ids") or []:
                try:
                    ids.add(int(i))
                except (TypeError, ValueError):
                    pass
            ids &= set(by_id)
            if task["id"] not in ids or len(ids) < SUGGEST_MIN_COUNT or conf < SUGGEST_MIN_CONFIDENCE:
                continue

            title = str(g.get("normalized_title") or task["text"]).strip()[:100]
            key = _norm_key(title)
            if not key or _already_handled(key, recs, sugs):
                continue

            freq = g.get("suggested_frequency")
            if freq not in FREQ_HINT:
                freq = "daily"

            group = [by_id[i] for i in ids]
            times = Counter(
                d.strftime("%H:%M") for d in (parse_due(t.get("due", "")) for t in group) if d
            )
            hhmm = times.most_common(1)[0][0] if times else "18:00"
            cats = Counter(t.get("category") for t in group if t.get("category"))
            labs = Counter(t.get("label") for t in group if t.get("label"))
            now_iso = datetime.now().isoformat()

            sid = await tasks_db.add_suggestion({
                "uid": uid,
                "key": key,
                "title": title,
                "frequency": freq,
                "time": hhmm,
                "category": cats.most_common(1)[0][0] if cats else "other",
                "label": labs.most_common(1)[0][0] if labs else "medium",
                "confidence": conf,
                "task_ids": sorted(ids),
                "status": "pending",
                "created_at": now_iso,
                "updated_at": now_iso,
            })
            await bot.send_message(
                uid,
                f"🔁 Помітив, що ти регулярно виконуєш: *{_md(title)}*.\n\n"
                f"Зробити це повторюваною таскою ({FREQ_HINT[freq]})?",
                reply_markup=ikb_recurring_suggestion(sid),
            )
            break
    except Exception:
        logger.exception("maybe_suggest_recurring failed for uid=%s", uid)