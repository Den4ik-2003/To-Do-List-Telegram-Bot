"""
ЗМІНЕНИЙ ФАЙЛ: handlers/settings.py

Додано (фіча "👥 Авторизовані користувачі" — блокування ПО ІМЕНІ, не по ID):
- settings_users_list: дістає всі uid з users_db.get_all_uids(), для
  кожного через bot.get_chat(uid) дістає реальне ім'я з Telegram (works,
  бо бот вже мав контакт із кожним авторизованим користувачем — інакше
  вони б не потрапили в auth_col). Якщо get_chat впав (юзер видалив акаунт
  тощо) — показує запасний варіант "Без імені (ID ...)", щоб такого
  користувача теж можна було прибрати.
- settings_block_ask:{uid}: екран підтвердження "Заблокувати {ім'я}? Так/Ні".
- settings_block_confirm:{uid}: викликає users_db.deauthorize(uid),
  повертає до оновленого списку.
- settings_back: повернення з підменю користувачів до головного меню
  налаштувань (на відміну від settings_close — не закриває, а показує
  меню налаштувань знову).

⚠️ ВАЖЛИВО: у поточному коді немає окремої ролі "адмін" — будь-який
авторизований користувач тепер бачить і може заблокувати БУДЬ-КОГО,
включно з іншими користувачами (себе заблокувати не можна — окрема
перевірка нижче). Якщо бот призначений не лише для вас особисто, варто
обмежити пункт "👥 Авторизовані користувачі" конкретним ADMIN_UID
(наприклад, перевіркою `if uid != ADMIN_UID: return await cb.answer(...)`
на початку settings_users_list) — скажіть, і я додам.

Решта хендлерів файлу — 1:1 як було.
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from database.mongo import DBUnavailable
from database import users as users_db
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.settings import (
    ikb_settings_menu,
    kb_currency_select,
    currency_from_text,
    ikb_users_list,
    ikb_block_confirm,
)
from handlers.common import require_auth
from config.constants import DB_ERROR_TEXT

logger = logging.getLogger("tasks_bot")
router = Router(name="settings")


class SettingsInput(StatesGroup):
    morning_time = State()
    ai_limit = State()
    currency = State()
    worktime_time = State()


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
        worktime_enabled=state.get("worktime_reminder_enabled", True),
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


@router.callback_query(F.data == "settings_toggle_worktime")
async def toggle_worktime(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        current = await users_db.get_user_state(uid)
        new_val = not current.get("worktime_reminder_enabled", True)
        await users_db.save_user_state(uid, {"worktime_reminder_enabled": new_val})
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    text, kb = await _render_settings(uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("🔔 Увімкнено" if new_val else "🔕 Вимкнено")


@router.callback_query(F.data == "settings_worktime_time")
async def ask_worktime_time(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsInput.worktime_time)
    await cb.message.answer(
        "⏰ Введи час нагадування про облік часу у форматі ГГ:ХХ (наприклад, 22:00):",
        reply_markup=kb_cancel(),
    )
    await cb.answer()


@router.message(SettingsInput.worktime_time)
async def save_worktime_time(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    text = msg.text.strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return await msg.answer("⚠️ Невірний формат. Введи час як ГГ:ХХ, наприклад 22:00.")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        return await msg.answer("⚠️ Невірний час. Введи час як ГГ:ХХ, наприклад 22:00.")
    try:
        await users_db.save_user_state(msg.from_user.id, {"worktime_reminder_time": f"{h:02d}:{m:02d}"})
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer(f"✅ Час нагадування про облік часу встановлено: {h:02d}:{m:02d}", reply_markup=kb_main())


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


# =========================================================
# НОВЕ: 👥 Авторизовані користувачі (список і блокування по імені)
# =========================================================

async def _display_name(cb: CallbackQuery, uid: int) -> str:
    """Дістає ім'я користувача напряму з Telegram (get_chat), бо в auth_col
    зберігається лише uid. Працює, бо бот вже мав контакт із кожним
    авторизованим користувачем."""
    try:
        chat = await cb.bot.get_chat(uid)
        name = (getattr(chat, "full_name", "") or "").strip()
        if not name and chat.username:
            name = f"@{chat.username}"
        return name or f"Без імені (ID {uid})"
    except Exception:
        logger.warning("Не вдалось дістати ім'я для uid %s через get_chat", uid, exc_info=True)
        return f"Без імені (ID {uid})"


async def _render_users_list(cb: CallbackQuery):
    uids = await users_db.get_all_uids()
    users = []
    for u in uids:
        name = await _display_name(cb, u)
        users.append({"uid": u, "name": name})
    users.sort(key=lambda x: x["name"].lower())
    text = "👥 *Авторизовані користувачі*\n\nНатисни на ім'я, щоб заблокувати:" if users \
        else "👥 *Авторизовані користувачі*\n\nСписок порожній."
    return text, ikb_users_list(users)


@router.callback_query(F.data == "settings_users_list")
async def settings_users_list(cb: CallbackQuery):
    try:
        text, kb = await _render_users_list(cb)
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # текст не змінився (наприклад, повторний клік "Ні" на тому ж списку)
    await cb.answer()


@router.callback_query(F.data == "settings_back")
async def settings_back(cb: CallbackQuery):
    text, kb = await _render_settings(cb.from_user.id)
    if text is None:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("settings_block_ask:"))
async def settings_block_ask(cb: CallbackQuery):
    target_uid = int(cb.data.split(":", 1)[1])
    if target_uid == cb.from_user.id:
        return await cb.answer("⚠️ Не можна заблокувати самого себе.", show_alert=True)
    name = await _display_name(cb, target_uid)
    await cb.message.edit_text(
        f"🚫 Заблокувати *{name}*?\n\nВін втратить доступ до бота.",
        reply_markup=ikb_block_confirm(target_uid),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("settings_block_confirm:"))
async def settings_block_confirm(cb: CallbackQuery):
    target_uid = int(cb.data.split(":", 1)[1])
    if target_uid == cb.from_user.id:
        return await cb.answer("⚠️ Не можна заблокувати самого себе.", show_alert=True)
    name = await _display_name(cb, target_uid)
    try:
        await users_db.deauthorize(target_uid)
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    try:
        text, kb = await _render_users_list(cb)
    except DBUnavailable:
        return await cb.answer(DB_ERROR_TEXT, show_alert=True)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(f"🚫 {name} заблоковано")