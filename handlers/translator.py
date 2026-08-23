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

TRANSLATE_LANGUAGES = {
    "pl": ("🇵🇱 Польська", "польську"),
    "en": ("🇬🇧 Англійська", "англійську"),
    "zh": ("🇨🇳 Китайська", "китайську"),
}


class Translate(StatesGroup):
    waiting_text = State()


class Rewrite(StatesGroup):
    waiting_text = State()


def _ikb_translate_lang() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"tr:{code}")] for code, (label, _) in TRANSLATE_LANGUAGES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    await msg.answer("Обери мову перекладу:", reply_markup=_ikb_translate_lang())


@router.callback_query(F.data.startswith("tr:"))
async def translate_pick_lang(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":", 1)[1]
    entry = TRANSLATE_LANGUAGES.get(code)
    if not entry:
        return await cb.answer()
    _, lang_name = entry

    fd = await state.get_data()
    text = fd.get("text", "")
    await state.clear()
    if not text:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Текст втрачено, спробуй ще раз.")

    await cb.answer("Перекладаю...")

    uid = cb.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await cb.message.edit_text(AI_LIMIT_TEXT)

    prompt = (
        f"Переклади наступний текст на {lang_name} мову. "
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
        "Напиши криво, як думаєш — я зроблю нормальне ввічливе повідомлення тією ж мовою.",
        reply_markup=kb_cancel(),
    )


@router.message(Rewrite.waiting_text)
async def rewrite_text_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    text = msg.text.strip()
    await state.clear()

    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    wait_msg = await msg.answer("✍️ Складаю повідомлення...")

    prompt = (
        f"Користувач написав неформально, можливо з помилками: \"{text}\". "
        "Визнач мову, якою це написано, і перепиши як ввічливе, грамотне повідомлення "
        "ТІЄЮ Ж САМОЮ мовою (не перекладай на іншу мову), збережи суть, зроби тон "
        "ввічливим/робочим. Поверни ЛИШЕ готове повідомлення, без пояснень."
    )
    result = await ai_service.generate_text(prompt, temperature=0.4)
    if not result:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(f"✍️ *Готове повідомлення:*\n\n{result}")
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())