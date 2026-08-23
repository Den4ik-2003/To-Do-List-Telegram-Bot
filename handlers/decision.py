import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config.constants import AI_ERROR_TEXT, AI_LIMIT_TEXT
from config.settings import AI_DAILY_LIMIT
from database import ai_usage as ai_usage_db
from services import ai_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="decision")


class Decision(StatesGroup):
    waiting_question = State()
    waiting_details = State()


@router.message(F.text == "⚖️ Рішення")
async def decision_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())
    await state.set_state(Decision.waiting_question)
    await msg.answer(
        "⚖️ *Помічник прийняття рішень*\n\n"
        "Опиши, між чим вибираєш, напр.: «купити BMW чи відкласти гроші».",
        reply_markup=kb_cancel(),
    )


@router.message(Decision.waiting_question)
async def decision_question_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(question=msg.text.strip())
    await state.set_state(Decision.waiting_details)
    await msg.answer(
        "Додай контекст, який вважаєш важливим (бюджет, терміни, пріоритети). "
        "Якщо немає — напиши «немає».",
        reply_markup=kb_cancel(),
    )


@router.message(Decision.waiting_details)
async def decision_details_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    fd = await state.get_data()
    question = fd.get("question", "")
    details = msg.text.strip()
    await state.clear()

    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    wait_msg = await msg.answer("⚖️ Аналізую варіанти...")

    prompt = f"""Ти — раціональний помічник прийняття рішень. Відповідай українською.

Питання користувача: {question}
Додатковий контекст: {details}

Дай структуровану відповідь у форматі:
✅ *Варіант 1*: ...
Плюси: ...
Мінуси: ...
Наслідки: ...

✅ *Варіант 2*: ...
Плюси: ...
Мінуси: ...
Наслідки: ...

🎯 *Рекомендація*: коротко, з поясненням чому.

Будь конкретним і чесним, не уникай прямої рекомендації."""

    result = await ai_service.generate_text(prompt, temperature=0.5)
    if not result:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(result)
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())