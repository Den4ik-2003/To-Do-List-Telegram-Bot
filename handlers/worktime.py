import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import worktime as worktime_db
from keyboards.worktime import (
    kb_worktime_menu, kb_cancel_wt, ikb_stats_menu, ikb_history_list,
    ikb_entry_actions, ikb_delete_confirm,
)
from handlers.common import require_auth
from services.worktime_service import (
    parse_hours, fmt_hours, fmt_date_display,
    build_today_report, build_week_report, build_month_report, build_period_report,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="worktime")

CANCEL_TEXT = "❌ Скасувати"


class WorktimeInput(StatesGroup):
    hours = State()
    manual_date = State()
    period_start = State()
    period_end = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


async def _cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_worktime_menu())


@router.message(F.text == "🕐 Облік часу")
async def worktime_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("🕐 *Облік робочого часу*\n\nОбери дію:", reply_markup=kb_worktime_menu())


@router.callback_query(F.data.startswith("wtenter:"))
async def wtenter_cb(cb: CallbackQuery, state: FSMContext):
    try:
        date_str = cb.data.split(":", 1)[1]
        await state.set_state(WorktimeInput.hours)
        await state.update_data(wt_date=date_str)
        await cb.message.answer(
            f"🕐 Скільки годин ти працював {fmt_date_display(date_str)}?\n_Наприклад: 8 або 7.5_",
            reply_markup=kb_cancel_wt(),
        )
        await cb.answer()
    except Exception:
        logger.exception("wtenter_cb failed")
        await _safe_alert(cb)


@router.message(F.text == "🕐 Сьогодні")
async def worktime_today_input(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    existing = await worktime_db.get_entry(msg.from_user.id, today)
    await state.set_state(WorktimeInput.hours)
    await state.update_data(wt_date=today)
    if existing:
        await msg.answer(
            f"Сьогодні вже внесено *{fmt_hours(existing['hours'])} год*.\nВведи нове значення, щоб змінити:",
            reply_markup=kb_cancel_wt(),
        )
    else:
        await msg.answer(
            "🕐 Скільки годин ти сьогодні працював?\n_Наприклад: 8 або 7.5_",
            reply_markup=kb_cancel_wt(),
        )


@router.message(StateFilter(WorktimeInput.hours))
async def worktime_hours_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    hours = parse_hours(msg.text)
    if hours is None:
        return await msg.answer(
            "⚠️ Введи коректну кількість годин (число від 0 до 24, наприклад 8 або 7.5):",
            reply_markup=kb_cancel_wt(),
        )
    fd = await state.get_data()
    date_str = fd.get("wt_date") or datetime.now().strftime("%Y-%m-%d")
    try:
        await worktime_db.upsert_entry(msg.from_user.id, date_str, hours)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_worktime_menu())
    await state.clear()
    await msg.answer(
        f"✅ Збережено: {fmt_date_display(date_str)} — {fmt_hours(hours)} год",
        reply_markup=kb_worktime_menu(),
    )


@router.message(F.text == "🗓 Історія годин")
async def worktime_history(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        entries = await worktime_db.get_recent_entries(msg.from_user.id, limit=60)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_worktime_menu())
    if not entries:
        await msg.answer("📭 Історія порожня.", reply_markup=kb_worktime_menu())
        await msg.answer("Можна додати запис за минулу дату:", reply_markup=ikb_history_list([]))
        return
    await msg.answer(f"🗓 *Історія* — {len(entries)} записів", reply_markup=kb_worktime_menu())
    await msg.answer("Обери запис:", reply_markup=ikb_history_list(entries))


@router.callback_query(F.data.startswith("wtpage:"))
async def worktime_history_page(cb: CallbackQuery):
    try:
        page = int(cb.data.split(":")[1])
        entries = await worktime_db.get_recent_entries(cb.from_user.id, limit=60)
        await cb.message.edit_reply_markup(reply_markup=ikb_history_list(entries, page))
        await cb.answer()
    except Exception:
        logger.exception("worktime_history_page failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "wtback_history")
async def worktime_back_history(cb: CallbackQuery):
    try:
        entries = await worktime_db.get_recent_entries(cb.from_user.id, limit=60)
        if not entries:
            await cb.message.edit_text("📭 Історія порожня.")
        else:
            await cb.message.edit_text(f"🗓 *Історія* — {len(entries)} записів", reply_markup=ikb_history_list(entries))
        await cb.answer()
    except Exception:
        logger.exception("worktime_back_history failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("wtview:"))
async def worktime_view_entry(cb: CallbackQuery):
    try:
        date_str = cb.data.split(":", 1)[1]
        entry = await worktime_db.get_entry(cb.from_user.id, date_str)
        if not entry:
            return await cb.answer("Не знайдено!", show_alert=True)
        text = f"🕐 *{fmt_date_display(date_str)}*\n\n{fmt_hours(entry['hours'])} год"
        await cb.message.edit_text(text, reply_markup=ikb_entry_actions(date_str))
        await cb.answer()
    except Exception:
        logger.exception("worktime_view_entry failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("wtedit:"))
async def worktime_edit_entry(cb: CallbackQuery, state: FSMContext):
    try:
        date_str = cb.data.split(":", 1)[1]
        await state.set_state(WorktimeInput.hours)
        await state.update_data(wt_date=date_str)
        await cb.message.answer(
            f"✏️ Введи нову кількість годин за {fmt_date_display(date_str)}:",
            reply_markup=kb_cancel_wt(),
        )
        await cb.answer()
    except Exception:
        logger.exception("worktime_edit_entry failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("wtdel:"))
async def worktime_delete_confirm(cb: CallbackQuery):
    try:
        date_str = cb.data.split(":", 1)[1]
        await cb.message.edit_text(
            f"🗑 Видалити запис за {fmt_date_display(date_str)}?",
            reply_markup=ikb_delete_confirm(date_str),
        )
        await cb.answer()
    except Exception:
        logger.exception("worktime_delete_confirm failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("wtdelconfirm:"))
async def worktime_delete_do(cb: CallbackQuery):
    try:
        date_str = cb.data.split(":", 1)[1]
        await worktime_db.delete_entry(cb.from_user.id, date_str)
        await cb.message.edit_text(f"🗑 Запис за {fmt_date_display(date_str)} видалено.")
        await cb.answer("Видалено!")
    except Exception:
        logger.exception("worktime_delete_do failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "wtaddmanual")
async def worktime_add_manual(cb: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(WorktimeInput.manual_date)
        await cb.message.answer("📅 Введи дату як *дд.мм.рррр*:", reply_markup=kb_cancel_wt())
        await cb.answer()
    except Exception:
        logger.exception("worktime_add_manual failed")
        await _safe_alert(cb)


@router.message(StateFilter(WorktimeInput.manual_date))
async def worktime_manual_date_input(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    try:
        parsed = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return await msg.answer(
            "⚠️ Невірний формат. Введи дату як *дд.мм.рррр*\n_Наприклад: 05.09.2026_",
            reply_markup=kb_cancel_wt(),
        )
    if parsed.date() > datetime.now().date():
        return await msg.answer("⚠️ Не можна внести години за майбутню дату. Введи іншу дату:", reply_markup=kb_cancel_wt())
    date_str = parsed.strftime("%Y-%m-%d")
    await state.set_state(WorktimeInput.hours)
    await state.update_data(wt_date=date_str)
    await msg.answer(
        f"🕐 Скільки годин ти працював {fmt_date_display(date_str)}?\n_Наприклад: 8 або 7.5_",
        reply_markup=kb_cancel_wt(),
    )


@router.message(F.text == "📊 Статистика годин")
async def worktime_stats_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("📊 Обери період:", reply_markup=ikb_stats_menu())


@router.callback_query(F.data == "wtstat:today")
async def wtstat_today(cb: CallbackQuery):
    try:
        text = await build_today_report(cb.from_user.id)
        await cb.message.edit_text(text)
        await cb.answer()
    except Exception:
        logger.exception("wtstat_today failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "wtstat:week")
async def wtstat_week(cb: CallbackQuery):
    try:
        text = await build_week_report(cb.from_user.id)
        await cb.message.edit_text(text)
        await cb.answer()
    except Exception:
        logger.exception("wtstat_week failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "wtstat:month")
async def wtstat_month(cb: CallbackQuery):
    try:
        text = await build_month_report(cb.from_user.id)
        await cb.message.edit_text(text)
        await cb.answer()
    except Exception:
        logger.exception("wtstat_month failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "wtstat:custom")
async def wtstat_custom(cb: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(WorktimeInput.period_start)
        await cb.message.answer("📅 Введи дату початку періоду як *дд.мм.рррр*:", reply_markup=kb_cancel_wt())
        await cb.answer()
    except Exception:
        logger.exception("wtstat_custom failed")
        await _safe_alert(cb)


@router.message(StateFilter(WorktimeInput.period_start))
async def wtstat_period_start(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    try:
        parsed = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return await msg.answer("⚠️ Невірний формат. Введи дату як *дд.мм.рррр*:", reply_markup=kb_cancel_wt())
    await state.update_data(wt_period_start=parsed.strftime("%Y-%m-%d"))
    await state.set_state(WorktimeInput.period_end)
    await msg.answer("📅 Введи дату завершення періоду як *дд.мм.рррр*:", reply_markup=kb_cancel_wt())


@router.message(StateFilter(WorktimeInput.period_end))
async def wtstat_period_end(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel(msg, state)
    raw = (msg.text or "").strip()
    try:
        parsed = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return await msg.answer("⚠️ Невірний формат. Введи дату як *дд.мм.рррр*:", reply_markup=kb_cancel_wt())
    fd = await state.get_data()
    start_date = fd.get("wt_period_start")
    end_date = parsed.strftime("%Y-%m-%d")
    await state.clear()
    if start_date and end_date < start_date:
        start_date, end_date = end_date, start_date
    text = await build_period_report(msg.from_user.id, start_date, end_date, f"{start_date} — {end_date}")
    await msg.answer(text, reply_markup=kb_worktime_menu())


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass