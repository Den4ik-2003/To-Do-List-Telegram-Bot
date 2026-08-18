import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery

from services import ai_service
from services.insights_service import generate_breakdown
from database import goals as goals_db

logger = logging.getLogger("tasks_bot")
router = Router(name="insights")


@router.callback_query(F.data.startswith("insight_breakdown:"))
async def insight_breakdown_cb(cb: CallbackQuery):
    gid = cb.data.split(":", 1)[1]
    await cb.answer("🤖 Аналізую...")

    if not ai_service.is_available():
        return await cb.message.answer("⚠️ AI тимчасово недоступний.")

    goal = await goals_db.get_goal(gid)
    if not goal:
        return await cb.message.answer("⚠️ Ціль не знайдено (можливо, вже видалена).")

    breakdown = await generate_breakdown({"goal": goal, "stale_days": 14})
    if not breakdown:
        return await cb.message.answer("⚠️ Не вдалося згенерувати розбір, спробуй пізніше.")

    await cb.message.answer(f"🧠 *Розбір цілі «{goal.get('title','')}»*\n\n{breakdown}")


@router.callback_query(F.data.startswith("insight_dismiss:"))
async def insight_dismiss_cb(cb: CallbackQuery):
    await cb.answer("Гаразд, повернусь до цього пізніше 👍")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass