import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from database import users as users_db
from services import planner_service
from handlers.ai_planner import generate_and_show_plan_for_message

logger = logging.getLogger("tasks_bot")
router = Router(name="morning_plan")


class AwaitingMorningTimeFilter(BaseFilter):
    async def __call__(self, message: Message, state: FSMContext) -> bool:
        if not message.text:
            return False
        if await state.get_state() is not None:
            return False
        user_state = await users_db.get_user_state(message.from_user.id)
        if not user_state.get("awaiting_morning_time"):
            return False
        today_str = datetime.now().strftime("%Y-%m-%d")
        if user_state.get("awaiting_morning_date") != today_str:
            return False
        return True


@router.message(AwaitingMorningTimeFilter())
async def morning_plan_answer(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    available = planner_service.parse_available_time(msg.text or "")
    if not available:
        return await msg.answer(
            "⚠️ Не зрозумів. Напиши кількість годин або часовий проміжок, наприклад:\n"
            "`3 години` або `2 години, з 19:00 до 21:00`"
        )
    await users_db.save_user_state(uid, {"awaiting_morning_time": False})
    await generate_and_show_plan_for_message(msg, available)