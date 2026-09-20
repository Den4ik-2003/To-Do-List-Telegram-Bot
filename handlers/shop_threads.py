import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from config.constants import AI_ERROR_TEXT, DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import shops as shops_db
from database import shop_threads as shop_threads_db
from services import ai_service
from services.thread_generator_service import generate_thread_ideas, format_ideas_message
from keyboards.shop_threads import ikb_thread_ideas_actions, ikb_threads_declined

logger = logging.getLogger("tasks_bot")
router = Router(name="shop_threads")


async def _shop_title(shop_id: str) -> str:
    shop = await shops_db.get_shop(shop_id)
    return (shop or {}).get("title", "Магазин")


async def _generate_and_show(target: Message, uid: int, shop_id: str):
    """Генерує Threads-ідеї для магазину і показує їх, редагуючи
    повідомлення `target`. Спільна логіка для кнопки «🔄 Нові ідеї»
    та для відповіді «✅ Так» на ранкове питання."""
    title = await _shop_title(shop_id)
    sample_products = await shop_threads_db.get_sample_product_names(shop_id)
    previous_texts = await shop_threads_db.get_recent_texts(shop_id)

    ideas = await generate_thread_ideas(title, sample_products, previous_texts)
    if not ideas:
        # Кнопки лишаємо, щоб можна було одразу натиснути «🔄 Нові ідеї» ще раз
        return await target.edit_text(AI_ERROR_TEXT, reply_markup=ikb_thread_ideas_actions(shop_id))

    await shop_threads_db.add_ideas(uid, shop_id, ideas)
    await target.edit_text(
        format_ideas_message(title, ideas),
        reply_markup=ikb_thread_ideas_actions(shop_id),
    )


@router.callback_query(F.data.startswith("shopthreads:"))
async def threads_menu_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        latest = await shop_threads_db.get_latest(shop_id)
        title = await _shop_title(shop_id)

        if not latest:
            await cb.message.edit_text(
                f"🧵 *Threads для «{title}»*\n\n"
                "Ще немає згенерованих ідей. Щоранку бот питатиме, чи потрібні треди сьогодні — "
                "або натисни «🔄 Нові ідеї», щоб отримати їх прямо зараз.",
                reply_markup=ikb_thread_ideas_actions(shop_id),
            )
        else:
            text = format_ideas_message(title, latest.get("ideas", []))
            await cb.message.edit_text(text, reply_markup=ikb_thread_ideas_actions(shop_id))
        await cb.answer()
    except DBUnavailable:
        await _safe_alert(cb)
    except Exception:
        logger.exception("threads_menu_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("threadsregen:"))
async def threads_regenerate_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        if not ai_service.is_available():
            await cb.answer()
            return await cb.message.answer(AI_ERROR_TEXT)

        await cb.answer("Генерую нові ідеї...")
        wait_msg = await cb.message.answer("🧵 Готую 3 нові Threads-ідеї...")
        await _generate_and_show(wait_msg, cb.from_user.id, shop_id)
    except DBUnavailable:
        await _safe_alert(cb)
    except Exception:
        logger.exception("threads_regenerate_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("threadsask:"))
async def threads_ask_answer_cb(cb: CallbackQuery):
    """Відповідь на ранкове питання «Потрібні сьогодні Threads-пости?».
    callback_data: threadsask:<yes|no>:<shop_id>"""
    try:
        _, answer, shop_id = cb.data.split(":", 2)

        if answer == "no":
            await cb.message.edit_text(
                "Ок, сьогодні без тредів 👌",
                reply_markup=ikb_threads_declined(shop_id),
            )
            return await cb.answer()

        if not ai_service.is_available():
            await cb.answer()
            return await cb.message.answer(AI_ERROR_TEXT)

        await cb.answer("Генерую...")
        # edit_text без reply_markup прибирає кнопки → подвійне натискання неможливе
        await cb.message.edit_text("🧵 Готую 3 Threads-ідеї...")
        await _generate_and_show(cb.message, cb.from_user.id, shop_id)
    except DBUnavailable:
        await _safe_alert(cb)
    except Exception:
        logger.exception("threads_ask_answer_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass