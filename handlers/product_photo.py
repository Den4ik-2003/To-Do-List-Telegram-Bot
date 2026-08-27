import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT, AI_LIMIT_TEXT, DEFAULT_CURRENCY
from config.settings import AI_DAILY_LIMIT
from database import ai_usage as ai_usage_db
from services import ai_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="product_photo")


class ProductPhoto(StatesGroup):
    waiting_photo = State()


def _ikb_retry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Спробувати ще раз", callback_data="product_retry"),
    ]])


@router.message(F.text == "📷 Фото → Товар")
async def product_photo_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())

    await state.set_state(ProductPhoto.waiting_photo)
    await msg.answer(
        "📷 *Фото → Товар*\n\n"
        "Сфотографуй товар — я визначу, що це, і напишу назву, опис та приблизну ціну "
        "для оголошення.",
        reply_markup=kb_cancel(),
    )


@router.message(ProductPhoto.waiting_photo, F.text == "❌ Скасувати")
async def product_photo_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(ProductPhoto.waiting_photo, F.photo)
async def product_photo_received(msg: Message, state: FSMContext, bot):
    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        await state.clear()
        return await msg.answer(AI_LIMIT_TEXT, reply_markup=kb_main())

    wait_msg = await msg.answer("📷 Аналізую товар...")

    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити фото товару для uid=%s", uid)
        await state.clear()
        await wait_msg.edit_text("⚠️ Не вдалося завантажити фото. Спробуй ще раз.")
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    data = await ai_service.analyze_product_photo(image_bytes)
    await state.clear()

    if not data or not data.get("title"):
        await wait_msg.edit_text(
            "🤔 Не вдалося розпізнати товар. Спробуй фото зі кращим освітленням або під іншим кутом."
        )
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    await ai_usage_db.increment_usage(uid)

    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    category = data.get("category", "").strip()
    price = data.get("price_uah")
    price_reasoning = data.get("price_reasoning", "").strip()

    price_line = (
        f"💵 *Приблизна ціна:* {price:.0f} {DEFAULT_CURRENCY}" if price
        else "💵 *Ціна:* не вдалося оцінити"
    )

    text = (
        "📷 *Готове оголошення:*\n\n"
        f"📝 *Назва:*\n{title}\n\n"
        f"📄 *Опис:*\n{description}\n\n"
        f"🏷 *Категорія:* {category}\n"
        f"{price_line}\n"
    )
    if price_reasoning:
        text += f"_{price_reasoning}_\n"

    await wait_msg.edit_text(text, reply_markup=_ikb_retry())
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.message(ProductPhoto.waiting_photo)
async def product_photo_wrong_content(msg: Message):
    await msg.answer("📷 Надішли, будь ласка, саме фото товару (як зображення).")