import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove

from config.constants import DB_ERROR_TEXT
from config.settings import BOT_PASSWORD
from database.mongo import DBUnavailable
from database import users as users_db
from keyboards.main_menu import kb_main
from keyboards.tasks import kb_tasks_menu
from keyboards.ai import ikb_ai_menu
from handlers.common import Auth, require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    try:
        authed = await users_db.is_authorized(msg.from_user.id)
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT)
        return
    if authed:
        await msg.answer(
            "🤖 *Personal AI Planner*\n\nОбери дію в меню знизу:",
            reply_markup=kb_main(),
        )
    else:
        await state.set_state(Auth.waiting_password)
        await msg.answer("🔒 *Доступ закритий*\n\nВведіть пароль:", reply_markup=ReplyKeyboardRemove())


@router.message(Auth.waiting_password)
async def check_password(msg: Message, state: FSMContext):
    if msg.text == BOT_PASSWORD:
        try:
            await users_db.authorize(msg.from_user.id)
        except DBUnavailable:
            await msg.answer(DB_ERROR_TEXT)
            return
        await state.clear()
        await msg.answer("✅ *Пароль вірний! Ласкаво просимо.*\n\nОбери дію:", reply_markup=kb_main())
    else:
        await msg.answer("❌ Невірний пароль. Спробуй ще раз:")


# =========================================================
# ГОЛОВНЕ МЕНЮ (кнопка "◀️ Головне меню" з reply-клавіатур усіх розділів)
# =========================================================
#
# ВАЖЛИВО: цього хендлера раніше не було в кодовій базі взагалі. Кнопка
# "◀️ Головне меню" є на клавіатурах tasks/finances/goals/projects тощо
# (kb_tasks_menu і т.п.), але жоден роутер не обробляв F.text для неї.
# Через це натискання на неї — коли немає активного FSM-стану — провалювалось
# до останнього catch-all хендлера в handlers/finances.py
# (quick_add_catch_all), який намагався розпізнати текст як фінансову
# транзакцію, не знаходив нічого і мовчки повертав None. Звідси і виглядало,
# ніби кнопка "деколи не працює" після довгої навігації.
#
# Реєструємо тут, у роутері "start", який (переконайся!) підключений в
# todo.py РАНІШЕ за роутер "finances" — інакше catch-all все одно перехопить
# повідомлення першим.
@router.message(F.text == "◀️ Головне меню")
async def back_to_main_menu(msg: Message, state: FSMContext):
    await state.clear()
    if not await require_auth(msg, state):
        return
    await msg.answer("🏠 *Головне меню*", reply_markup=kb_main())


@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("🏠 *Головне меню*", reply_markup=kb_main())


@router.message(Command("tasks"))
async def cmd_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("📋 *Мої задачі*", reply_markup=kb_tasks_menu())


@router.message(Command("goals"))
async def cmd_goals(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю розділ цілей... Натисни «🎯 Мої цілі» в меню.", reply_markup=kb_main())


@router.message(Command("projects"))
async def cmd_projects(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю розділ проєктів... Натисни «📁 Мої проєкти» в меню.", reply_markup=kb_main())


@router.message(Command("finance"))
async def cmd_finance(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю фінанси... Натисни «💰 Фінанси» в меню.", reply_markup=kb_main())


@router.message(Command("ai"))
async def cmd_ai(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("🤖 *AI Планер*\n\nЩо зробити?", reply_markup=ikb_ai_menu())


@router.message(Command("chat"))
async def cmd_chat(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю AI Чат... Натисни «💬 AI Чат» в меню.", reply_markup=kb_main())


@router.message(Command("stats"))
async def cmd_stats(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю статистику... Натисни «📊 Статистика» в меню.", reply_markup=kb_main())


@router.message(Command("settings"))
async def cmd_settings(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer("Відкриваю налаштування... Натисни «⚙️ Налаштування» в меню.", reply_markup=kb_main())