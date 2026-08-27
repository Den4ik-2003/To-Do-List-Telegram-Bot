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
router = Router(name="movie")


class MovieMood(StatesGroup):
    waiting_mood = State()


@router.message(F.text == "🎬 Що подивитися сьогодні")
async def movie_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())
    await state.set_state(MovieMood.waiting_mood)
    await msg.answer(
        "🎬 *Що подивитися сьогодні*\n\n"
        "Опиши настрій чи побажання своїми словами, напр.:\n"
        "«хочу щось як Breaking Bad, але коротке і не дуже важке» "
        "або «легка комедія на вечір, щоб не думати».",
        reply_markup=kb_cancel(),
    )


@router.message(MovieMood.waiting_mood)
async def movie_mood_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    mood = msg.text.strip()
    await state.clear()

    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    wait_msg = await msg.answer("🎬 Підбираю варіанти...")

    prompt = f"""Ти — досвідчений кінокритик-порадник. Відповідай українською.

Користувач описав, що хоче подивитися: "{mood}"

Запропонуй 3-4 КОНКРЕТНІ фільми або серіали (реальні, що дійсно існують),
які відповідають опису. Для кожного:
- Назва (і рік, якщо це важливо для впізнавання)
- 1-2 речення чому це підходить під запит (спирайся на конкретні деталі
  запиту: тон, тривалість, складність, схожість на приклад, якщо він був)
- Приблизна тривалість серії/фільму або кількість сезонів

Не пиши загальних фраз на кшталt "усім сподобається". Якщо в запиті є
приклад (наприклад "як Х, але Y") — явно поясни, чим твоя пропозиція схожа
на приклад і чим відрізняється відповідно до побажання Y.

Формат:
🎬 *Назва (рік)*
Чому підходить: ...
⏱ Тривалість: ...

Не додавай нічого зайвого до і після списку варіантів."""

    result = await ai_service.generate_text(prompt, temperature=0.6)
    if not result:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(result)