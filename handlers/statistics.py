import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import DB_ERROR_TEXT, AI_ERROR_TEXT, AI_LIMIT_TEXT, DEFAULT_CURRENCY
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import users as users_db
from services import statistics_service
from services import planner_service
from services import ai_service
from keyboards.main_menu import kb_main
from handlers.common import require_auth, level_progress, compute_daily_stats

logger = logging.getLogger("tasks_bot")
router = Router(name="statistics")


def _ikb_stats() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌙 Аналіз дня", callback_data="stat_daily_analysis")],
        [InlineKeyboardButton(text="📆 Підсумок тижня", callback_data="stat_weekly")],
    ])


@router.message(F.text == "📊 Статистика")
async def stats_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    uid = msg.from_user.id
    try:
        overview = await statistics_service.get_overview(uid)
        user_state = await users_db.get_user_state(uid)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    xp = user_state.get("xp", 0)
    level, into_level, threshold = level_progress(xp)
    bar_len = 12
    filled = int(bar_len * into_level / threshold) if threshold else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    total_completed = user_state.get("total_completed", 0)
    total_missed = user_state.get("total_missed", 0)
    denom = total_completed + total_missed
    completion = round(total_completed / denom * 100) if denom else 100

    text = (
        "📊 *Статистика*\n\n"
        "*Задачі:*\n"
        f"✅ {overview['tasks_done']} виконано\n"
        f"⏳ {overview['tasks_active']} залишилось\n"
        f"⚠️ {overview['tasks_overdue']} прострочено\n"
        f"📈 Відсоток виконання: {completion}%\n\n"
        "*Цілі та проєкти:*\n"
        f"🎯 {overview['goals_active']} активних цілей\n"
        f"📁 {overview['projects_active']} активних проєктів\n\n"
        "*Фінанси:*\n"
        f"💰 Баланс: {overview['balance']:,.0f} {DEFAULT_CURRENCY}\n\n".replace(",", " ") +
        "*AI активність:*\n"
        f"🤖 {overview['ai_limit'] - overview['ai_remaining']} / {overview['ai_limit']} запитів сьогодні\n\n"
        f"🔥 Серія: {user_state.get('streak', 0)} днів\n\n"
        f"🏆 Level {level}\n{bar}\n{into_level} / {threshold} XP"
    )
    await msg.answer(text, reply_markup=kb_main())
    await msg.answer("Хочеш детальніший AI-аналіз?", reply_markup=_ikb_stats())


@router.callback_query(F.data == "stat_daily_analysis")
async def stat_daily_analysis_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    if not ai_service.is_available():
        return await cb.message.edit_text(AI_ERROR_TEXT)
    allowed, _ = await planner_service.check_ai_limit(uid)
    if not allowed:
        return await cb.message.edit_text(AI_LIMIT_TEXT)
    try:
        await cb.answer("Аналізую...")
        await cb.message.edit_text("🌙 Аналізую твій день...")
        stats = await compute_daily_stats(uid, tasks_db)
        text = await planner_service.generate_daily_analysis(uid, stats)
        if not text:
            return await cb.message.edit_text(AI_ERROR_TEXT)
        header = f"🌙 *Аналіз дня*\n\n✅ Виконано: {stats['done_count']}\n❌ Не виконано: {stats['missed_count']}\n\n"
        await cb.message.edit_text(header + text)
    except Exception:
        logger.exception("stat_daily_analysis_cb failed")
        await _safe_edit(cb, AI_ERROR_TEXT)


@router.callback_query(F.data == "stat_weekly")
async def stat_weekly_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        await cb.answer("Аналізую тиждень...")
        await cb.message.edit_text("📆 Аналізую твій тиждень...")
        weekly = await statistics_service.get_weekly_summary(uid)

        header = (
            "📆 *Підсумок тижня*\n\n"
            f"✅ Виконано задач: {weekly['done_count']}\n"
            f"❌ Пропущено задач: {weekly['missed_count']}\n"
            f"📈 Дохід: {weekly['income']:,.0f} {DEFAULT_CURRENCY}\n".replace(",", " ") +
            f"📉 Витрати: {weekly['expense']:,.0f} {DEFAULT_CURRENCY}\n".replace(",", " ") +
            f"💵 Чистий результат: {weekly['net']:,.0f} {DEFAULT_CURRENCY}\n".replace(",", " ") +
            f"📁 Активних проєктів: {weekly['active_projects_count']}\n"
        )

        if ai_service.is_available():
            allowed, _ = await planner_service.check_ai_limit(uid)
            if allowed:
                ai_text = await planner_service.generate_weekly_analysis(uid, weekly)
                if ai_text:
                    header += f"\n🤖 *AI підсумок:*\n{ai_text}"

        await cb.message.edit_text(header)
    except Exception:
        logger.exception("stat_weekly_cb failed")
        await _safe_edit(cb, AI_ERROR_TEXT)


async def _safe_edit(cb: CallbackQuery, text: str):
    try:
        await cb.message.edit_text(text)
    except TelegramAPIError:
        pass