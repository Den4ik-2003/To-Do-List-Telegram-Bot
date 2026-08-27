import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from database import job_profile as job_profile_db
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="job_profile")


class JobProfile(StatesGroup):
    profession = State()
    experience = State()
    skills = State()
    education = State()
    languages = State()
    desired_salary = State()
    location = State()
    work_format = State()
    employment_type = State()
    resume_summary = State()


STEPS = [
    (JobProfile.profession, "profession", "👤 Яка твоя професія/спеціальність?"),
    (JobProfile.experience, "experience", "💼 Досвід роботи (скільки років, де)?"),
    (JobProfile.skills, "skills", "🛠 Ключові навички (через кому)?"),
    (JobProfile.education, "education", "🎓 Освіта?"),
    (JobProfile.languages, "languages", "🌐 Мови (рівень)?"),
    (JobProfile.desired_salary, "desired_salary", "💰 Бажана зарплата?"),
    (JobProfile.location, "location", "📍 Бажана локація?"),
    (JobProfile.work_format, "work_format", "💻 Формат роботи (remote/office/hybrid)?"),
    (JobProfile.employment_type, "employment_type", "🕐 Тип зайнятості (повна/неповна)?"),
    (JobProfile.resume_summary, "resume_summary", "📄 Коротко про себе (як для резюме, 2-4 речення)?"),
]


@router.message(F.text == "👤 Мої дані для пошуку")
async def profile_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(STEPS[0][0])
    await msg.answer(
        "👤 *Мої дані для пошуку роботи*\n\nЗаповни один раз — і я використовуватиму це "
        "при кожному пошуку вакансій та генерації cover letter.\n\n" + STEPS[0][2],
        reply_markup=kb_cancel(),
    )


def _find_step_index(current_state: str) -> int:
    for i, (state_cls, _, _) in enumerate(STEPS):
        if state_cls.state == current_state:
            return i
    return -1


@router.message(
    JobProfile.profession, JobProfile.experience, JobProfile.skills, JobProfile.education,
    JobProfile.languages, JobProfile.desired_salary, JobProfile.location,
    JobProfile.work_format, JobProfile.employment_type, JobProfile.resume_summary,
)
async def profile_step(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    current = await state.get_state()
    idx = _find_step_index(current)
    if idx == -1:
        await state.clear()
        return await msg.answer("Щось пішло не так, спробуй ще раз.", reply_markup=kb_main())

    _, field, _ = STEPS[idx]
    await state.update_data(**{field: msg.text.strip()})

    if idx + 1 < len(STEPS):
        next_state, _, next_prompt = STEPS[idx + 1]
        await state.set_state(next_state)
        await msg.answer(next_prompt, reply_markup=kb_cancel())
    else:
        data = await state.get_data()
        await state.clear()
        await job_profile_db.save_profile(msg.from_user.id, data)
        await msg.answer(
            "✅ Дані збережено! Тепер можеш шукати вакансії — напиши, наприклад:\n"
            "«Знайди Frontend вакансії від 1000$ remote»",
            reply_markup=kb_main(),
        )