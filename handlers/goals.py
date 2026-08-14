import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import PRIORITY_EMOJI, GOAL_ACTIVE, DB_ERROR_TEXT, DEFAULT_CURRENCY
from database.mongo import DBUnavailable
from database import goals as goals_db
from services import goal_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.goals import (
    kb_priority, priority_from_text, kb_goal_type, goal_type_from_text,
    ikb_goals, ikb_goal_actions,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="goals")

GOAL_TYPE_MAP = {"financial": "financial", "simple": "general"}


class AddGoal(StatesGroup):
    title = State()
    description = State()
    goal_type = State()
    target_amount = State()
    priority = State()


class UpdateGoalProgress(StatesGroup):
    amount = State()


def _fmt_goals_list(goals: list) -> str:
    if not goals:
        return (
            "🎯 *Мої цілі*\n\n"
            "Ще немає жодної цілі.\n"
            "Натисни «➕ Додати ціль», щоб задати першу — AI буде враховувати її при плануванні."
        )
    lines = ["🎯 *Мої цілі*", ""]
    for g in goals:
        active = g.get("status") == GOAL_ACTIVE
        status = "🟢 Активна" if active else "⏸ Неактивна"
        pr = PRIORITY_EMOJI.get(g.get("priority", "medium"), "🟡")
        lines.append(f"{pr} *{g.get('title','')}* — {status}")
        if g.get("goal_type") == "financial" and g.get("target_amount"):
            percent = goal_service.progress_percent(g)
            bar = goal_service.progress_bar(percent)
            lines.append(f"   {bar} {percent}%")
            lines.append(f"   {g.get('current_amount', 0)} / {g.get('target_amount')} {DEFAULT_CURRENCY}")
        if g.get("description"):
            lines.append(f"   _{g['description'][:80]}_")
        lines.append("")
    return "\n".join(lines).strip()


def _fmt_goal_detail(g: dict) -> str:
    active = g.get("status") == GOAL_ACTIVE
    status = "🟢 Активна" if active else "⏸ Неактивна"
    pr = PRIORITY_EMOJI.get(g.get("priority", "medium"), "🟡")
    lines = [f"🎯 *{g.get('title','')}*", "", f"Статус: {status}", f"Пріоритет: {pr} {g.get('priority','medium')}"]
    if g.get("description"):
        lines.append(f"\n📝 {g['description']}")
    if g.get("goal_type") == "financial" and g.get("target_amount"):
        percent = goal_service.progress_percent(g)
        bar = goal_service.progress_bar(percent)
        lines.append("")
        lines.append(f"{bar} {percent}%")
        lines.append(f"{g.get('current_amount', 0)} / {g.get('target_amount')} {DEFAULT_CURRENCY}")
        remaining = max(0, (g.get("target_amount") or 0) - (g.get("current_amount") or 0))
        lines.append(f"Залишилось: {remaining} {DEFAULT_CURRENCY}")
    if g.get("deadline"):
        lines.append(f"\n📅 Дедлайн: {g['deadline']}")
    return "\n".join(lines)


def _with_active_flag(goals: list) -> list:
    for g in goals:
        g["active"] = g.get("status") == GOAL_ACTIVE
    return goals


@router.message(F.text == "🎯 Мої цілі")
async def goals_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        goals = _with_active_flag(await goals_db.get_all_goals(msg.from_user.id))
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer(_fmt_goals_list(goals), reply_markup=ikb_goals(goals))


@router.callback_query(F.data == "goals_close")
async def goals_close_cb(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await cb.answer()


@router.callback_query(F.data == "ai_goals")
async def ai_goals_back_cb(cb: CallbackQuery):
    """Кнопка «◀️ До списку» в ikb_goal_actions веде сюди ж, що і меню цілей."""
    try:
        goals = _with_active_flag(await goals_db.get_all_goals(cb.from_user.id))
        await cb.message.edit_text(_fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


@router.callback_query(F.data.startswith("goalopen:"))
async def goal_open_cb(cb: CallbackQuery):
    try:
        gid = cb.data.split(":", 1)[1]
        g = await goals_db.get_goal(gid)
        if not g:
            return await cb.answer("Не знайдено", show_alert=True)
        active = g.get("status") == GOAL_ACTIVE
        await cb.message.edit_text(_fmt_goal_detail(g), reply_markup=ikb_goal_actions(gid, active))
        await cb.answer()
    except Exception:
        logger.exception("goal_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("goaltoggle:"))
async def goal_toggle_cb(cb: CallbackQuery):
    try:
        gid = cb.data.split(":", 1)[1]
        g = await goals_db.get_goal(gid)
        if not g:
            return await cb.answer("Не знайдено", show_alert=True)
        new_active = g.get("status") != GOAL_ACTIVE
        await goal_service.toggle_goal_status(gid, new_active)
        goals = _with_active_flag(await goals_db.get_all_goals(cb.from_user.id))
        await cb.message.edit_text(_fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer("Оновлено")
    except Exception:
        logger.exception("goal_toggle_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("goaldel:"))
async def goal_delete_cb(cb: CallbackQuery):
    try:
        gid = cb.data.split(":", 1)[1]
        await goal_service.remove_goal(gid)
        goals = _with_active_flag(await goals_db.get_all_goals(cb.from_user.id))
        await cb.message.edit_text(_fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer("Видалено")
    except Exception:
        logger.exception("goal_delete_cb failed")
        await _safe_alert(cb)


# =========================================================
# ДОДАВАННЯ ЦІЛІ
# =========================================================

@router.callback_query(F.data == "goal_add")
async def goal_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddGoal.title)
    await cb.message.answer("🎯 Введи назву цілі (коротко):", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AddGoal.title)
async def goal_add_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(title=msg.text.strip()[:100])
    await state.set_state(AddGoal.description)
    await msg.answer("📝 Опиши ціль детальніше (або напиши «-», щоб пропустити):", reply_markup=kb_cancel())


@router.message(AddGoal.description)
async def goal_add_desc(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:300]
    await state.update_data(description=desc)
    await state.set_state(AddGoal.goal_type)
    await msg.answer("Це фінансова ціль (з конкретною сумою) чи звичайна?", reply_markup=kb_goal_type())


@router.message(AddGoal.goal_type)
async def goal_add_type(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    goal_type_raw = goal_type_from_text(msg.text)
    if not goal_type_raw:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_goal_type())
    goal_type = GOAL_TYPE_MAP[goal_type_raw]
    await state.update_data(goal_type=goal_type)
    if goal_type == "financial":
        await state.set_state(AddGoal.target_amount)
        return await msg.answer(f"💰 Яка цільова сума в {DEFAULT_CURRENCY}? (наприклад 1000000)", reply_markup=kb_cancel())
    await state.set_state(AddGoal.priority)
    await msg.answer("🎚 Обери пріоритет цілі:", reply_markup=kb_priority())


@router.message(AddGoal.target_amount)
async def goal_add_target_amount(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await msg.answer("⚠️ Введи додатнє число, наприклад 1000000:", reply_markup=kb_cancel())
    await state.update_data(target_amount=amount)
    await state.set_state(AddGoal.priority)
    await msg.answer("🎚 Обери пріоритет цілі:", reply_markup=kb_priority())


@router.message(AddGoal.priority)
async def goal_add_priority(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    priority = priority_from_text(msg.text)
    if not priority:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_priority())
    fd = await state.get_data()
    try:
        await goal_service.create_goal(
            uid=msg.from_user.id,
            title=fd["title"],
            description=fd.get("description", ""),
            priority=priority,
            goal_type=fd.get("goal_type", "general"),
            target_amount=fd.get("target_amount"),
        )
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer("✅ Ціль додано! AI буде враховувати її при плануванні та в порадах.", reply_markup=kb_main())


# =========================================================
# ОНОВЛЕННЯ ПРОГРЕСУ (для фінансових цілей)
# =========================================================

@router.callback_query(F.data.startswith("goalupdate:"))
async def goal_update_start(cb: CallbackQuery, state: FSMContext):
    gid = cb.data.split(":", 1)[1]
    g = await goals_db.get_goal(gid)
    if not g:
        return await cb.answer("Не знайдено", show_alert=True)
    if g.get("goal_type") != "financial":
        return await cb.answer("Ця ціль не фінансова — оновлення суми не потрібне.", show_alert=True)
    await state.set_state(UpdateGoalProgress.amount)
    await state.update_data(goal_id=gid)
    await cb.message.answer(
        f"💰 На скільки {DEFAULT_CURRENCY} посунувся прогрес? "
        f"(введи число, від'ємне — щоб зменшити)",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(UpdateGoalProgress.amount)
async def goal_update_amount(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(raw)
    except ValueError:
        return await msg.answer("⚠️ Введи число, наприклад 5000 або -1000:", reply_markup=kb_cancel())
    fd = await state.get_data()
    gid = fd["goal_id"]
    try:
        await goal_service.add_progress(gid, amount)
        g = await goals_db.get_goal(gid)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    if g:
        active = g.get("status") == GOAL_ACTIVE
        await msg.answer(f"✅ Оновлено!\n\n{_fmt_goal_detail(g)}", reply_markup=kb_main())
    else:
        await msg.answer("✅ Оновлено!", reply_markup=kb_main())


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass