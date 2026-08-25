import logging
import secrets
from datetime import datetime, timedelta

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
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.tasks import (
    kb_tasks_menu, kb_label, label_from_text, kb_category, category_from_text,
    kb_date, ikb_task_actions, ikb_edit_fields, ikb_tasks_list, ikb_categories,
)
from handlers.common import (
    require_auth, user_list_cache, fmt_task, fmt_due, parse_due,
    is_today, is_missed, sort_tasks, sort_tasks_by_label_then_due, level_progress,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="tasks")

CANCEL_TEXT = "❌ Скасувати"


class AddTask(StatesGroup):
    text = State()
    label = State()
    category = State()
    date = State()
    date_manual = State()
    time = State()


class EditField(StatesGroup):
    typing = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


async def _cancel(msg: Message, state: FSMContext, kb=None) -> None:
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb or kb_tasks_menu())


# =========================================================
# ВХІД У РОЗДІЛ
# =========================================================

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


# =========================================================
# ДОДАВАННЯ ЗАДАЧІ
# =========================================================

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
    await state.set_state(AddTask.date)
    await msg.answer("📅 Коли треба це зробити? Оберіть дату:", reply_markup=kb_date())


@router.message(StateFilter(AddTask.date))
async def at_date(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
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
        "due": fmt_due(due_dt),
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
        "project_id": None,
        "estimated_minutes": None,
    }
    try:
        await tasks_db.add_task(task)
        saved = await tasks_db.get_task(new_id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())

    if not saved:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())
    await msg.answer(f"✅ *Завдання додано!*\n\n{fmt_task(saved)}", reply_markup=kb_tasks_menu())


# =========================================================
# СПИСКИ
# =========================================================

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
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer()
    except Exception:
        logger.exception("view_task failed")
        await _safe_alert(cb)


# =========================================================
# ДІЇ НАД ЗАДАЧЕЮ
# =========================================================

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
        if new_level > old_level:
            extra += f"\n🏆 Новий рівень: *{new_level}*!"

        try:
            await cb.message.edit_text(f"✅ *Виконано!*\n\n{fmt_task(t)}{extra}")
        except TelegramAPIError:
            await cb.message.answer(f"✅ *Виконано!*\n\n{fmt_task(t)}{extra}")
        await cb.answer("✅ Виконано!")
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
    try:
        await cb.message.edit_text(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid, t))
    except TelegramAPIError:
        await cb.message.answer(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid, t))
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
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
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
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer("📌 Відкріплено")
    except Exception:
        logger.exception("unpin_task failed")
        await _safe_alert(cb)


# =========================================================
# ПІДЗАДАЧІ
# =========================================================

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


# =========================================================
# РЕДАГУВАННЯ
# =========================================================

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
        try:
            datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *дд.мм.рррр*:", reply_markup=kb_cancel())
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
        await msg.answer(f"✅ Оновлено!\n\n{fmt_task(t)}", reply_markup=kb_tasks_menu())
    else:
        await msg.answer("Завдання не знайдено.", reply_markup=kb_tasks_menu())


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass