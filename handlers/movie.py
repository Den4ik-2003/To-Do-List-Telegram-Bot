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
        "або «легка комедія на вечір, щоб не думати».\n\n"
        "Можеш питати скільки завгодно разів підряд — я не вийду в головне "
        "меню сам, натисни «❌ Скасувати», коли захочеш завершити.",
        reply_markup=kb_cancel(),
    )


@router.message(MovieMood.waiting_mood)
async def movie_mood_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    mood = (msg.text or "").strip()
    if not mood:
        # Порожній текст (напр. стікер без caption) — просимо ввести текст,
        # стан лишається активним, кнопка "Скасувати" й далі робоча.
        return await msg.answer("⚠️ Опиши текстом, що хочеш подивитися:", reply_markup=kb_cancel())

    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        # Ліміт AI вичерпано — продовжувати цикл сенсу нема, виходимо
        # в головне меню самі (це єдиний випадок примусового виходу).
        await state.clear()
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    # Стан НЕ чистимо тут — юзер має лишитись у режимі "питати про фільми"
    # незалежно від результату нижче.
    wait_msg = await msg.answer("🎬 Підбираю варіанти...")

    prompt = f"""Ти — досвідчений кінокритик-порадник. Відповідай українською.

Користувач описав, що хоче подивитися: "{mood}"

Запропонуй 3-4 КОНКРЕТНІ фільми або серіали (реальні, що дійсно існують),
які відповідають опису. Для кожного:
- Назва (і рік, якщо це важливо для впізнавання)
- 1-2 речення чому це підходить під запит (спирайся на конкретні деталі
  запиту: тон, тривалість, складність, схожість на приклад, якщо він був)
- Приблизна тривалість серії/фільму або кількість сезонів

Не пиши загальних фраз на кшталт "усім сподобається". Якщо в запиті є
приклад (наприклад "як Х, але Y") — явно поясни, чим твоя пропозиція схожа
на приклад і чим відрізняється відповідно до побажання Y.

Формат:
🎬 *Назва (рік)*
Чому підходить: ...
⏱ Тривалість: ...

Не додавай нічого зайвого до і після списку варіантів."""

    result = await ai_service.generate_text(prompt, temperature=0.6)
    if not result:
        # Тимчасовий збій AI — НЕ викидаємо в головне меню, дозволяємо
        # спробувати ще раз одразу ж, стан і далі активний.
        await wait_msg.edit_text(AI_ERROR_TEXT)
        await msg.answer("Можеш спробувати ще раз або натисни «❌ Скасувати»:", reply_markup=kb_cancel())
        return

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(result)

    # ВАЖЛИВО: стан лишається MovieMood.waiting_mood — це і є цикл.
    # Юзер може одразу писати новий запит; єдиний вихід у головне меню —
    # явне натискання "❌ Скасувати" (перевіряється на початку хендлера)
    # або вичерпання денного ліміту AI вище.
    await msg.answer("Питай ще, або натисни «❌ Скасувати», щоб завершити:", reply_markup=kb_cancel())