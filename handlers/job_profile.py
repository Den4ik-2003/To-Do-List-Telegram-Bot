"""
ЗМІНЕНИЙ ФАЙЛ: handlers/job_profile.py

НОВЕ:
- Розширений профіль: додано рівень, проєкти/досягнення, курси/сертифікати,
  готовність до переїзду, бажані сфери, «що не підходить», дату старту.
  Список і порядок полів беремо з database.job_profile.PROFILE_FIELDS.
- Якщо профіль уже є — замість повного перепитування показуємо, на скільки
  він заповнений, і даємо вибір: «➕ Доповнити порожні» (питаємо лише те,
  чого бракує), «🔄 Заповнити все заново» або «✖️ Закрити».
- Лічильник прогресу (3/7) у кожному питанні.
- Відповідь «-» = свідомо порожньо: поле запам'ятовується в skipped_fields
  і більше не питається в режимі «Доповнити порожні».
- Захист від не-текстових повідомлень (голосове/фото) на кроках анкети.
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config.settings import JOB_MIN_MATCH_PERCENT
from database import job_profile as job_profile_db
from database.job_profile import PROFILE_FIELDS, PROFILE_LABELS
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.jobs import ikb_profile_menu
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="job_profile")


class JobProfile(StatesGroup):
    profession = State()
    level = State()
    experience = State()
    skills = State()
    projects = State()
    education = State()
    certifications = State()
    languages = State()
    desired_salary = State()
    location = State()
    relocation = State()
    work_format = State()
    employment_type = State()
    industries = State()
    dealbreakers = State()
    available_from = State()
    portfolio_url = State()
    linkedin = State()
    github = State()
    resume_summary = State()


PROMPTS = {
    "profession": "👤 Яка твоя професія/спеціальність?",
    "level": "📊 Твій рівень? (без досвіду / junior / middle / senior / lead)",
    "experience": "💼 Досвід роботи (скільки років, де)?",
    "skills": "🛠 Ключові навички та технології (через кому)?",
    "projects": "🚀 Ключові проєкти або досягнення (1–3 пункти, з результатами, якщо є)?",
    "education": "🎓 Освіта?",
    "certifications": "📜 Курси та сертифікати? (або «-» якщо немає)",
    "languages": "🌐 Мови (рівень)?",
    "desired_salary": "💰 Бажана зарплата?",
    "location": "📍 Бажана локація?",
    "relocation": "✈️ Готовність до переїзду або відряджень? (так / ні / лише в межах країни)",
    "work_format": "💻 Формат роботи (remote/office/hybrid)?",
    "employment_type": "🕐 Тип зайнятості (повна/неповна)?",
    "industries": "🏭 Бажані сфери/галузі (напр. e-commerce, fintech, ігри)? (або «-» якщо байдуже)",
    "dealbreakers": "🚫 Що точно НЕ підходить? (галузі, технології, умови — через кому; або «-»)",
    "available_from": "📅 Коли готовий почати? (одразу / через 2 тижні / дата)",
    "portfolio_url": "🔗 Посилання на портфоліо? (або «-» якщо немає)",
    "linkedin": "💼 LinkedIn? (або «-»)",
    "github": "🐙 GitHub? (або «-»)",
    "resume_summary": "📄 Коротко про себе (як для резюме, 2-4 речення)?",
}

# Помилка тут = невідповідність між PROFILE_FIELDS і JobProfile/PROMPTS —
# краще впасти при старті бота, ніж посеред анкети.
FIELD_STATE = {field: getattr(JobProfile, field) for field in PROFILE_FIELDS}
_missing_prompts = [f for f in PROFILE_FIELDS if f not in PROMPTS]
assert not _missing_prompts, f"Немає тексту питання для полів: {_missing_prompts}"


def _field_by_state(current_state: str) -> str | None:
    for field, state_cls in FIELD_STATE.items():
        if state_cls.state == current_state:
            return field
    return None


async def _ask(message: Message, state: FSMContext, queue: list[str], position: int, header: str = ""):
    field = queue[position]
    await state.set_state(FIELD_STATE[field])
    await message.answer(
        f"{header}*({position + 1}/{len(queue)})* {PROMPTS[field]}",
        reply_markup=kb_cancel(),
    )


async def _begin_flow(message: Message, state: FSMContext, queue: list[str], header: str = ""):
    await state.set_data({"_queue": queue, "_answers": {}})
    await _ask(message, state, queue, 0, header)


@router.message(F.text == "👤 Мої дані для пошуку")
async def profile_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return

    profile = await job_profile_db.get_profile(msg.from_user.id)
    status = job_profile_db.get_profile_status(profile)

    # Профіль ще порожній — проходимо всю анкету
    if status["done"] == 0:
        return await _begin_flow(
            msg,
            state,
            list(PROFILE_FIELDS),
            header=(
                "👤 *Мої дані для пошуку роботи*\n\nЗаповни один раз — і я використовуватиму це "
                "при кожному пошуку вакансій та генерації cover letter.\n\n"
            ),
        )

    text = (
        "👤 *Мої дані для пошуку роботи*\n\n"
        f"Профіль заповнено на *{status['percent']}%* ({status['done']} з {status['total']} полів)."
    )
    if status["missing"]:
        names = ", ".join(PROFILE_LABELS[f] for f in status["missing"])
        text += f"\n\nНе вистачає: {names}."
    text += (
        "\n\nЩо повніший профіль — то точніше рахується збіг. "
        f"Показую лише вакансії, які підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%."
    )
    await msg.answer(text, reply_markup=ikb_profile_menu(len(status["missing"])))


@router.callback_query(F.data.startswith("jprof:"))
async def profile_menu_cb(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":", 1)[1]

    if action == "close":
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        return await cb.answer()

    if action == "missing":
        profile = await job_profile_db.get_profile(cb.from_user.id)
        queue = job_profile_db.get_profile_status(profile)["missing"]
        if not queue:
            return await cb.answer("Усе вже заповнено 👍", show_alert=True)
    elif action == "all":
        queue = list(PROFILE_FIELDS)
    else:
        return await cb.answer()

    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await _begin_flow(cb.message, state, queue)


@router.message(StateFilter(*FIELD_STATE.values()))
async def profile_step(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    if not msg.text:
        return await msg.answer("Надішли відповідь текстом 🙂", reply_markup=kb_cancel())

    field = _field_by_state(await state.get_state())
    if field is None:
        await state.clear()
        return await msg.answer("Щось пішло не так, спробуй ще раз.", reply_markup=kb_main())

    data = await state.get_data()
    queue: list[str] = data.get("_queue") or list(PROFILE_FIELDS)
    answers: dict = dict(data.get("_answers") or {})

    value = msg.text.strip()
    answers[field] = "" if value == "-" else value
    await state.update_data(_answers=answers)

    position = queue.index(field) if field in queue else len(queue) - 1
    if position + 1 < len(queue):
        return await _ask(msg, state, queue, position + 1)

    # Остання відповідь — зберігаємо
    uid = msg.from_user.id
    old_profile = await job_profile_db.get_profile(uid) or {}
    skipped = set(old_profile.get("skipped_fields") or [])
    for f, v in answers.items():
        if v:
            skipped.discard(f)
        else:
            skipped.add(f)

    payload = dict(answers)
    payload["skipped_fields"] = sorted(skipped)

    await state.clear()
    await job_profile_db.save_profile(uid, payload)

    status = job_profile_db.get_profile_status(await job_profile_db.get_profile(uid))
    await msg.answer(
        f"✅ Дані збережено! Профіль заповнено на {status['percent']}%.\n\n"
        f"Показуватиму лише вакансії, які підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%.\n"
        "Тепер можеш шукати вакансії — напиши, наприклад:\n"
        "«Знайди Frontend вакансії від 1000$ remote»",
        reply_markup=kb_main(),
    )