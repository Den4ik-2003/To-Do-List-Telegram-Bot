import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from database.mongo import DBUnavailable
from database import users as users_db
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.settings import ikb_settings_menu, kb_currency_select, currency_from_text
from handlers.common import require_auth
from config.constants import DB_ERROR_TEXT

logger = logging.getLogger("tasks_bot")
router = Router(name="settings")


class SettingsInput(StatesGroup):
    morning_time = State()
    ai_limit = State()
    currency = State()


def _settings_text() -> str:
    return "⚙️ *Налаштування*\n\nОбери, що хочеш змінити:"


async def _render_settings(uid: int):
    try:
        state = await users_db.get_user_state(uid)
    except DBUnavailable:
        return None, None
    kb = ikb_settings_menu(
        morning_enabled=state.get("ai_morning_enabled", True),
        evening_enabled=state.get("ai_evening_enabled", True),
        notifications_enabled=state.get("notifications_enabled", True),
    )
    return _settings_text(), kb


@router.message(F.text == "⚙️ Налаштування")
async def settings_open(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    text, kb = await _render_settings(msg.from_user.id)
    if text is None:
        return await msg.answer(DB_ERROR_TEXT)
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "settings_toggle_morning")
async def toggle_morning(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        current = await users_db.get_user_state(uid)
        new_val = not current.get("ai_morning_enabled", True)
        await users_db.save_user_state(uid, {"ai_morning_enabled": new_val})
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    text, kb = await _render_settings(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("🔔 Увімкнено" if new_val else "🔕 Вимкнено")


@router.callback_query(F.data == "settings_toggle_evening")
async def toggle_evening(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        current = await users_db.get_user_state(uid)
        new_val = not current.get("ai_evening_enabled", True)
        await users_db.save_user_state(uid, {"ai_evening_enabled": new_val})
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    text, kb = await _render_settings(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("🌙 Увімкнено" if new_val else "🌙 Вимкнено")


@router.callback_query(F.data == "settings_toggle_notifications")
async def toggle_notifications(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        current = await users_db.get_user_state(uid)
        new_val = not current.get("notifications_enabled", True)
        await users_db.save_user_state(uid, {"notifications_enabled": new_val})
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    text, kb = await _render_settings(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("🔔 Увімкнено" if new_val else "🔕 Вимкнено")


@router.callback_query(F.data == "settings_morning_time")
async def ask_morning_time(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsInput.morning_time)
    await cb.message.answer(
        "⏰ Введи час ранкового плану у форматі ГГ:ХХ (наприклад, 09:00):",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(SettingsInput.morning_time)
async def save_morning_time(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    text = msg.text.strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return await msg.answer("⚠️ Невірний формат. Введи час як ГГ:ХХ, наприклад 09:00.")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        return await msg.answer("⚠️ Невірний час. Введи час як ГГ:ХХ, наприклад 09:00.")
    try:
        await users_db.save_user_state(msg.from_user.id, {"ai_morning_time": f"{h:02d}:{m:02d}"})
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer(f"✅ Час ранкового плану встановлено: {h:02d}:{m:02d}", reply_markup=kb_main())


@router.callback_query(F.data == "settings_ai_limit")
async def show_ai_limit_info(cb: CallbackQuery):
    from config.settings import AI_DAILY_LIMIT
    await cb.message.answer(f"📊 Поточний ліміт AI-запитів на день: *{AI_DAILY_LIMIT}*")
    await cb.answer()


@router.callback_query(F.data == "settings_ai")
async def show_ai_settings_info(cb: CallbackQuery):
    from config.settings import AI_MODEL
    await cb.message.answer(f"🤖 Поточна AI-модель: `{AI_MODEL}`")
    await cb.answer()


@router.callback_query(F.data == "settings_currency")
async def ask_currency(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsInput.currency)
    await cb.message.answer("💰 Обери валюту:", reply_markup=kb_currency_select())
    await cb.answer()


@router.message(SettingsInput.currency)
async def save_currency(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    currency = currency_from_text(msg.text)
    if not currency:
        return await msg.answer("⚠️ Обери валюту з кнопок нижче.")
    try:
        await users_db.save_user_state(msg.from_user.id, {"currency": currency})
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer(f"✅ Валюту встановлено: {currency}", reply_markup=kb_main())


@router.callback_query(F.data == "settings_close")
async def close_settings(cb: CallbackQuery):
    await cb.message.delete()
    await cb.message.answer("🏠 *Головне меню*", reply_markup=kb_main())
    await cb.answer()