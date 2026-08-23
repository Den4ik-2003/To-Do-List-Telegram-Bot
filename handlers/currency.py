import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from database import rates as rates_db
from services import currency_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="currency")


class Converter(StatesGroup):
    waiting_input = State()


@router.message(F.text == "💱 Курс валют")
async def show_rates(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return

    doc = await rates_db.get_rates()
    if not doc:
        doc = await currency_service.update_rates()
    if not doc:
        return await msg.answer("⚠️ Не вдалося отримати курси валют. Спробуй пізніше.", reply_markup=kb_main())

    updated = (doc.get("updated_at") or "")[:16].replace("T", " ")
    text = (
        "💱 *Курс валют (НБУ)*\n\n"
        f"🇺🇸 USD: *{doc.get('USD', 0):.2f}* грн\n"
        f"🇪🇺 EUR: *{doc.get('EUR', 0):.2f}* грн\n"
        f"🇵🇱 PLN: *{doc.get('PLN', 0):.2f}* грн\n"
        f"🇨🇳 CNY: *{doc.get('CNY', 0):.2f}* грн\n\n"
        f"🕐 Оновлено: {updated}"
    )
    await msg.answer(text, reply_markup=kb_main())


@router.message(F.text == "💰 Конвертер")
async def converter_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(Converter.waiting_input)
    await msg.answer(
        "💰 *Конвертер валют*\n\n"
        "Напиши суму і валюти, наприклад:\n"
        "`1500 PLN → UAH`\n"
        "`200$ в PLN`\n"
        "`300 CNY в UAH`\n\n"
        "Підтримувані валюти: USD, EUR, PLN, CNY, UAH.",
        reply_markup=kb_cancel(),
    )


@router.message(Converter.waiting_input)
async def converter_input(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    result = await currency_service.try_convert(msg.text or "")
    if not result:
        return await msg.answer("🤔 Не зрозумів формат. Спробуй так: `1500 PLN → UAH`")
    await state.clear()
    await msg.answer(result, reply_markup=kb_main())