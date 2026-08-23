import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT, AI_ERROR_TEXT, AI_LIMIT_TEXT, DEFAULT_CURRENCY
from config.settings import AI_DAILY_LIMIT
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import goals as goals_db
from database import projects as projects_db
from database import finances as finances_db
from database import ai_usage as ai_usage_db
from services import ai_service
from services import statistics_service
from keyboards.main_menu import kb_main, kb_category, MAIN_CATEGORIES, BACK_TO_MAIN
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="menu")


@router.message(F.text == BACK_TO_MAIN)
async def back_to_main(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.clear()
    await msg.answer("🏠 *Головне меню*", reply_markup=kb_main())


@router.message(F.text.in_(MAIN_CATEGORIES.keys()))
async def open_category(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    category = msg.text
    await msg.answer(f"{category}:", reply_markup=kb_category(category))


@router.callback_query(F.data == "noop")
async def noop_cb(cb: CallbackQuery):
    await cb.answer()


# =========================================================
# 💡 ПОРАДИ — конкретні AI-поради на основі реальних даних
# =========================================================

@router.message(F.text == "💡 Поради")
async def advice_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    uid = msg.from_user.id

    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    wait_msg = await msg.answer("💡 Аналізую твої дані, зачекай кілька секунд...")

    try:
        tasks = await tasks_db.get_user_tasks(uid)
        active = [t for t in tasks if t.get("status") == "pending"]
        overdue = [t for t in active if _is_missed(t)]
        goals = await goals_db.get_active_goals(uid)
        projects = await projects_db.get_active_projects(uid)
        balance_summary = await _balance_summary(uid)
    except DBUnavailable:
        return await wait_msg.edit_text(DB_ERROR_TEXT)

    if not tasks and not goals and not projects:
        return await wait_msg.edit_text(
            "💡 *Поради*\n\nУ мене поки недостатньо даних для точної рекомендації.\n"
            "Додай хоча б кілька задач, ціль або проєкт — і я зможу дати конкретні поради."
        )

    goals_text = "\n".join(
        f"- {g.get('title','')}" + (
            f" ({g.get('current_amount',0)}/{g.get('target_amount')} {DEFAULT_CURRENCY})"
            if g.get("goal_type") == "financial" and g.get("target_amount") else ""
        )
        for g in goals
    ) or "(немає)"
    projects_text = "\n".join(f"- {p.get('title','')}" for p in projects) or "(немає)"

    prompt = f"""Ти — персональний AI-радник. Дай 2-4 КОНКРЕТНІ поради українською на основі реальних даних нижче.
НЕ пиши загальні мотиваційні фрази. Кожна порада повинна спиратись на конкретну цифру чи факт з даних.

Активних задач: {len(active)}
Прострочених задач: {len(overdue)}
Цілі: {goals_text}
Проєкти: {projects_text}
Баланс: {balance_summary['balance']} {DEFAULT_CURRENCY}
Дохід цього місяця: {balance_summary['month_income']} {DEFAULT_CURRENCY}
Витрати цього місяця: {balance_summary['month_expense']} {DEFAULT_CURRENCY}

Формат відповіді — список порад, кожна з нового рядка, починається з "💡 ".
Не додавай нічого зайвого до і після списку."""

    text = await ai_service.generate_text(prompt, temperature=0.5)
    if not text:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(f"💡 *Поради на основі твоїх даних*\n\n{text}")


def _is_missed(t: dict) -> bool:
    from handlers.common import is_missed
    return is_missed(t)


async def _balance_summary(uid: int) -> dict:
    balance = await finances_db.get_balance(uid)
    now = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")
    month_end = now.strftime("%Y-%m-%dT23:59:59")
    month = await finances_db.get_period_summary(uid, month_start, month_end)
    return {"balance": balance, "month_income": month["income"], "month_expense": month["expense"]}