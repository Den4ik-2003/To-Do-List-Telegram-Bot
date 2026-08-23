import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT, AI_LIMIT_TEXT
from config.settings import AI_DAILY_LIMIT
from database import ai_usage as ai_usage_db
from services import ai_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="translator")


class Translate(StatesGroup):
    waiting_text = State()


class Rewrite(StatesGroup):
    waiting_text = State()


def _ikb_lang(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇵🇱 Польська", callback_data=f"{prefix}:pl"),
        InlineKeyboardButton(text="🇬🇧 Англійська", callback_data=f"{prefix}:en"),
    ]])


@router.message(F.text == "🌐 Переклад")
async def translate_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())
    await state.set_state(Translate.waiting_text)
    await msg.answer(
        "🌐 *AI Перекладач*\n\nНадішли текст українською — оберу мову перекладу.",
        reply_markup=kb_cancel(),
    )


@router.message(Translate.waiting_text)
async def translate_text_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(text=msg.text)
    await msg.answer("Обери мову перекладу:", reply_markup=_ikb_lang("tr"))


@router.callback_query(F.data.startswith("tr:"))
async def translate_pick_lang(cb: CallbackQuery, state: FSMContext):
    lang = cb.data.split(":", 1)[1]
    fd = await state.get_data()
    text = fd.get("text", "")
    await state.clear()
    if not text:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Текст втрачено, спробуй ще раз.")

    lang_name = "польську" if lang == "pl" else "англійську"
    await cb.answer("Перекладаю...")

    uid = cb.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await cb.message.edit_text(AI_LIMIT_TEXT)

    prompt = (
        f"Переклади наступний текст з української на {lang_name} мову. "
        "Поверни ЛИШЕ переклад, без пояснень і лапок:\n\n" + text
    )
    result = await ai_service.generate_text(prompt, temperature=0.3)
    if not result:
        return await cb.message.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await cb.message.edit_text(f"🌐 *Переклад:*\n\n{result}")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.message(F.text == "✍️ Редактор")
async def rewrite_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())
    await state.set_state(Rewrite.waiting_text)
    await msg.answer(
        "✍️ *AI Редактор повідомлень*\n\n"
        "Напиши криво, як думаєш — я перетворю на нормальне ввічливе повідомлення.",
        reply_markup=kb_cancel(),
    )


@router.message(Rewrite.waiting_text)
async def rewrite_text_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(text=msg.text)
    await msg.answer("Якою мовою написати повідомлення?", reply_markup=_ikb_lang("rw"))


@router.callback_query(F.data.startswith("rw:"))
async def rewrite_pick_lang(cb: CallbackQuery, state: FSMContext):
    lang = cb.data.split(":", 1)[1]
    fd = await state.get_data()
    text = fd.get("text", "")
    await state.clear()
    if not text:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Текст втрачено, спробуй ще раз.")

    lang_name = "польській" if lang == "pl" else "англійській"
    await cb.answer("Складаю повідомлення...")

    uid = cb.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await cb.message.edit_text(AI_LIMIT_TEXT)

    prompt = (
        f"Користувач написав українською неформально, з помилками: \"{text}\". "
        f"Перепиши це як ввічливе, грамотне повідомлення на {lang_name} мові, "
        "збережи суть і зроби тон робочим/ввічливим. Поверни ЛИШЕ готове повідомлення."
    )
    result = await ai_service.generate_text(prompt, temperature=0.4)
    if not result:
        return await cb.message.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await cb.message.edit_text(f"✍️ *Готове повідомлення:*\n\n{result}")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())