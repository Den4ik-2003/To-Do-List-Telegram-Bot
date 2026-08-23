import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import EXPENSE_CATEGORIES, TRANSACTION_EXPENSE, AI_ERROR_TEXT
from database import finances as finances_db
from services import ai_service, currency_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="receipts")


class ReceiptFlow(StatesGroup):
    waiting_photo = State()


_pending_receipts: dict[int, dict] = {}


def _ikb_receipt_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Додати у витрати", callback_data="receipt_confirm"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="receipt_cancel"),
    ]])


@router.message(F.text == "🧾 Чек")
async def receipt_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())

    await state.set_state(ReceiptFlow.waiting_photo)
    await msg.answer(
        "🧾 *Розпізнавання чеків*\n\nНадішли фото чека (українською або польською) — "
        "я витягну суму, товари й категорію.",
        reply_markup=kb_cancel(),
    )


@router.message(ReceiptFlow.waiting_photo, F.text == "❌ Скасувати")
async def receipt_cancel_text(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(ReceiptFlow.waiting_photo, F.photo)
async def receipt_photo(msg: Message, state: FSMContext, bot):
    wait_msg = await msg.answer("🧾 Розпізнаю чек...")

    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити фото чека для uid=%s", msg.from_user.id)
        await state.clear()
        await wait_msg.edit_text("⚠️ Не вдалося завантажити фото. Спробуй ще раз.")
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    data = await ai_service.extract_receipt(image_bytes)
    await state.clear()

    if not data or not data.get("total"):
        await wait_msg.edit_text(
            "🤔 Не вдалося розпізнати чек. Спробуй чіткіше фото або введи витрату вручну через «💰 Фінанси».",
        )
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    category = data.get("category") or "other"
    if category not in EXPENSE_CATEGORIES:
        category = "other"
    currency = (data.get("currency") or "UAH").upper()
    items = data.get("items") or []
    total = float(data.get("total") or 0)

    uah_total = total
    if currency != "UAH":
        converted = await currency_service.convert_to_uah(total, currency)
        if converted is not None:
            uah_total = converted

    _pending_receipts[msg.from_user.id] = {
        "total": total,
        "uah_total": uah_total,
        "currency": currency,
        "category": category,
        "items": items,
    }

    cat_info = EXPENSE_CATEGORIES.get(category, {"emoji": "🗂", "name": "Інше"})
    items_text = ", ".join(items[:8]) if items else "(не розпізнано)"
    convert_line = ""
    if currency != "UAH":
        convert_line = f"💱 ≈ {uah_total:.2f} грн\n"

    text = (
        "🧾 *Розпізнано чек:*\n\n"
        f"💵 Сума: *{total:.2f} {currency}*\n"
        f"{convert_line}"
        f"🏷 Категорія: {cat_info['emoji']} {cat_info['name']}\n"
        f"🛒 Товари: {items_text}\n\n"
        "Додати цю витрату у фінанси?"
    )
    await wait_msg.edit_text(text, reply_markup=_ikb_receipt_confirm())
    await msg.answer("🏠 Або натисни кнопку нижче, щоб вийти без збереження:", reply_markup=kb_main())


@router.message(ReceiptFlow.waiting_photo)
async def receipt_wrong_content(msg: Message, state: FSMContext):
    await msg.answer("📷 Надішли, будь ласка, саме фото чека (як зображення).")


@router.callback_query(F.data == "receipt_confirm")
async def receipt_confirm_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = _pending_receipts.pop(uid, None)
    if not data:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Дані чека застаріли, спробуй ще раз.")

    description = ", ".join(data["items"][:8]) if data["items"] else "Чек (AI)"
    await finances_db.add_transaction(
        uid,
        TRANSACTION_EXPENSE,
        data["uah_total"],
        data["category"],
        description=description,
        date=datetime.now().isoformat(),
    )
    await cb.answer("Додано ✅")
    await cb.message.edit_text(f"✅ Витрату {data['uah_total']:.2f} грн додано у фінанси.")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.callback_query(F.data == "receipt_cancel")
async def receipt_cancel_cb(cb: CallbackQuery):
    _pending_receipts.pop(cb.from_user.id, None)
    await cb.answer()
    await cb.message.edit_text("❌ Скасовано, витрату не додано.")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())