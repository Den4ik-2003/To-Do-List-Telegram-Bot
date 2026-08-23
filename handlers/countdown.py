import logging
from datetime import date

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import events as events_db
from services import countdown_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="countdown")


class CountdownAdd(StatesGroup):
    waiting_name = State()
    waiting_date = State()


_pending_name: dict[int, str] = {}


def _ikb_events_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати подію", callback_data="countdown_add")],
    ])


@router.message(F.text == "📅 Дні до дати")
async def countdown_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return

    events = await events_db.get_user_events(msg.from_user.id)
    if not events:
        text = (
            "📅 *Дні до дати*\n\n"
            "У тебе поки немає збережених подій.\n\n"
            "Можеш просто написати мені:\n"
            "`скільки днів до 1 січня`\n\n"
            "Або додай подію, яку хочеш відстежувати постійно (напр. зарплату)."
        )
    else:
        lines = ["📅 *Твої події:*", ""]
        for ev in events:
            target = events_db.next_event_date(ev)
            if not target:
                continue
            days = (target - date.today()).days
            lines.append(f"• {ev['name']} — *{days} дн.* ({target.strftime('%d.%m.%Y')})")
        lines.append("")
        lines.append("Або просто напиши: `скільки днів до 1 січня`")
        text = "\n".join(lines)

    await msg.answer(text, reply_markup=_ikb_events_actions())


@router.callback_query(F.data == "countdown_add")
async def countdown_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CountdownAdd.waiting_name)
    await cb.message.answer(
        "✏️ Як назвати подію? (напр. «Зарплата», «Приїзд додому»)",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(CountdownAdd.waiting_name)
async def countdown_add_name(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    name = (msg.text or "").strip()
    if not name:
        return await msg.answer("Напиши, будь ласка, назву текстом.")
    _pending_name[msg.from_user.id] = name
    await state.set_state(CountdownAdd.waiting_date)
    await msg.answer(
        "📅 Тепер напиши дату:\n\n"
        "• `1 січня` — разова дата цього/наступного року\n"
        "• `1.01.2027` — конкретна дата\n"
        "• `щомісяця 25` — щомісячна подія (напр. зарплата 25 числа)"
    )


@router.message(CountdownAdd.waiting_date)
async def countdown_add_date(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        _pending_name.pop(msg.from_user.id, None)
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    text = (msg.text or "").strip().lower()
    name = _pending_name.get(msg.from_user.id)
    if not name:
        await state.clear()
        return await msg.answer("Щось пішло не так, почни спочатку.", reply_markup=kb_main())

    monthly = countdown_service.parse_monthly(text)
    if monthly:
        await events_db.add_event(msg.from_user.id, name, day=monthly, recurring="monthly")
        await state.clear()
        _pending_name.pop(msg.from_user.id, None)
        return await msg.answer(f"✅ Подію «{name}» додано (щомісяця {monthly} числа).", reply_markup=kb_main())

    parsed = countdown_service.parse_date_for_event(text)
    if not parsed:
        return await msg.answer("🤔 Не зрозумів дату. Спробуй ще раз: `1 січня` або `щомісяця 25`.")

    day, month, year = parsed
    recurring = "once" if year else "yearly"
    await events_db.add_event(msg.from_user.id, name, day=day, month=month, year=year, recurring=recurring)
    await state.clear()
    _pending_name.pop(msg.from_user.id, None)
    await msg.answer(f"✅ Подію «{name}» додано.", reply_markup=kb_main())