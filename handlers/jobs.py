import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from database import job_profile as job_profile_db
from database import jobs as jobs_db
from services import jobs_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="jobs")

_results_cache: dict[int, list[dict]] = {}
_criteria_cache: dict[int, dict] = {}


class JobSearch(StatesGroup):
    waiting_query = State()


def _ikb_vacancy_actions(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Зберегти", callback_data=f"jb_save:{idx}"),
        InlineKeyboardButton(text="✉️ Cover Letter", callback_data=f"jb_cover:{idx}"),
    ]])


def _fmt_vacancy(v: dict, score: dict) -> str:
    lines = [
        f"💼 *{v.get('title','')}*",
        f"🏢 {v.get('company') or '—'}",
    ]
    if v.get("location"):
        lines.append(f"📍 {v['location']}")
    if v.get("work_format"):
        lines.append(f"💻 {v['work_format']}")
    if v.get("salary"):
        lines.append(f"💰 {v['salary']}")
    lines.append(f"🔗 {v.get('url','')}")
    lines.append(f"_(джерело: {v.get('source','')})_")

    if score.get("match_percent") is not None:
        lines.append(f"\n🎯 Match: {score['match_percent']}%")
        if score.get("fits"):
            lines.append("✅ " + "; ".join(score["fits"][:3]))
        if score.get("missing"):
            lines.append("⚠️ " + "; ".join(score["missing"][:3]))
        if score.get("advice"):
            lines.append(f"📌 {score['advice']}")

    return "\n".join(lines)


async def _run_search(msg: Message, uid: int, query_text: str):
    profile = await job_profile_db.get_profile(uid)

    wait_msg = await msg.answer("🔎 Аналізую запит і шукаю вакансії...")
    criteria = await jobs_service.parse_job_query(query_text, profile)
    if not criteria:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return await wait_msg.edit_text(
            "📭 Нічого не знайшов за цим запитом. Спробуй ширші критерії."
        )

    await wait_msg.edit_text(f"✅ Знайдено {len(vacancies)} вакансій. Аналізую відповідність...")

    scored = []
    for v in vacancies[:10]:
        score = await jobs_service.score_vacancy(v, profile)
        v["_score"] = score
        scored.append(v)

    scored.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

    _results_cache[uid] = scored
    _criteria_cache[uid] = criteria

    for i, v in enumerate(scored):
        await msg.answer(_fmt_vacancy(v, v["_score"]), reply_markup=_ikb_vacancy_actions(i))

    if not profile:
        await msg.answer(
            "💡 Заповни «👤 Мої дані для пошуку» — і я зможу оцінювати відповідність вакансій "
            "та писати персональні cover letter."
        )

    await msg.answer("Що далі?", reply_markup=_ikb_search_menu())


def _ikb_search_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Стежити за цим пошуком", callback_data="jb_watch")],
    ])


@router.message(F.text == "🔎 Знайти вакансії")
async def jobs_search_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(JobSearch.waiting_query)
    await msg.answer(
        "💼 *Пошук вакансій*\n\nНапиши, яку роботу шукаєш, своїми словами. Напр.:\n"
        "«Шукаю Frontend Developer, React, Junior, remote»\n"
        "«Шукаю водія категорії B»\n"
        "«Знайди роботу продавцем у Львові»",
        reply_markup=kb_cancel(),
    )


@router.message(JobSearch.waiting_query)
async def jobs_search_query(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.clear()
    await _run_search(msg, msg.from_user.id, msg.text.strip())


@router.message(F.text.regexp(r"^\s*шукаю\b", flags=__import__("re").IGNORECASE))
async def jobs_natural_shukau(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _run_search(msg, msg.from_user.id, msg.text.strip())


@router.message(F.text.regexp(r"знайди.*(вакансі|роботу)", flags=__import__("re").IGNORECASE))
async def jobs_natural_znaidy(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _run_search(msg, msg.from_user.id, msg.text.strip())


@router.callback_query(F.data.startswith("jb_save:"))
async def jobs_save_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    v = results[idx]
    v["match_percent"] = v["_score"].get("match_percent")
    await jobs_db.save_vacancy(uid, v)
    await cb.answer("Збережено ⭐")


@router.callback_query(F.data.startswith("jb_cover:"))
async def jobs_cover_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    profile = await job_profile_db.get_profile(uid)
    if not profile:
        await cb.answer()
        return await cb.message.answer(
            "⚠️ Спочатку заповни «👤 Мої дані для пошуку» — cover letter пишеться на основі цих даних."
        )

    await cb.answer("Пишу cover letter...")
    v = results[idx]
    letter = await jobs_service.generate_cover_letter(v, profile)
    if not letter:
        return await cb.message.answer(AI_ERROR_TEXT)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Перегенерувати", callback_data=f"jb_cover:{idx}"),
    ]])
    await cb.message.answer(f"✉️ *Cover Letter:*\n\n{letter}", reply_markup=kb)


@router.callback_query(F.data == "jb_watch")
async def jobs_watch_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    criteria = _criteria_cache.get(uid)
    results = _results_cache.get(uid) or []
    if not criteria:
        return await cb.answer("Спочатку зроби пошук.", show_alert=True)

    seen_ids = [v["id"] for v in results]
    await jobs_db.add_search_watch(uid, criteria, seen_ids)
    await cb.answer("Додано до моніторингу 🔔", show_alert=True)


@router.message(F.text == "⭐ Збережені вакансії")
async def jobs_saved_list(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    saved = await jobs_db.get_saved(msg.from_user.id)
    if not saved:
        return await msg.answer("📭 Ще немає збережених вакансій.", reply_markup=kb_main())

    for v in saved:
        text = f"⭐ *{v.get('title','')}*\n🏢 {v.get('company') or '—'}\n🔗 {v.get('url','')}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"jb_del:{v['_id']}"),
        ]])
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("jb_del:"))
async def jobs_delete_cb(cb: CallbackQuery):
    sid = cb.data.split(":", 1)[1]
    ok = await jobs_db.delete_saved(sid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Видалено зі збережених.")
        except Exception:
            pass


@router.message(F.text == "🔔 Мої монітори вакансій")
async def jobs_watches_list(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    watches = await jobs_db.get_user_watches(msg.from_user.id)
    if not watches:
        return await msg.answer("📭 Немає активних моніторів вакансій.", reply_markup=kb_main())

    for w in watches:
        criteria = w.get("criteria", {})
        text = f"🔔 {criteria.get('profession','')} | {criteria.get('city') or 'будь-де'}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"jbw_del:{w['_id']}"),
        ]])
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("jbw_del:"))
async def jobs_watch_delete_cb(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    ok = await jobs_db.delete_watch(wid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)