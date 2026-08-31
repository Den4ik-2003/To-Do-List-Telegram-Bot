import logging
import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from database import job_profile as job_profile_db
from database import jobs as jobs_db
from services import jobs_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.jobs import (
    ikb_vacancy_card, ikb_not_interested_reasons, ikb_filters_menu,
    ikb_saved_item, ikb_watch_item,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="jobs")

_results_cache: dict[int, list[dict]] = {}
_criteria_cache: dict[int, dict] = {}
_position_cache: dict[int, int] = {}


class JobSearch(StatesGroup):
    waiting_query = State()


def _fmt_vacancy_card(v: dict, total_shown: int, position: int) -> str:
    score = v.get("_score", {})
    lines = [f"💼 *{v.get('title','')}*", ""]
    lines.append(f"🏢 {v.get('company') or '—'}")
    if v.get("location") or v.get("work_format"):
        loc = " / ".join(x for x in [v.get("location"), v.get("work_format")] if x)
        lines.append(f"📍 {loc}")
    if v.get("salary"):
        lines.append(f"💰 {v['salary']}")

    if score.get("match_percent") is not None:
        lines.append(f"\n🎯 Match: *{score['match_percent']}%*\n")
        for tag in (score.get("fits") or [])[:4]:
            lines.append(f"🟢 {tag}")
        for tag in (score.get("missing") or [])[:2]:
            lines.append(f"🟡 {tag}")

    sources = v.get("sources") or [v.get("source", "")]
    lines.append(f"\n🔗 Джерела: {' · '.join(s for s in sources if s)}")

    if score.get("advice"):
        lines.append(f"\n💡 {score['advice']}")

    lines.append(f"\n_{position + 1} з {total_shown}_")
    return "\n".join(lines)


async def _run_search(msg: Message, uid: int, query_text: str):
    profile = await job_profile_db.get_profile(uid)
    feedback = await jobs_db.get_recent_feedback(uid)

    wait_msg = await msg.answer("🔎 Аналізую запит і шукаю вакансії...")
    criteria = await jobs_service.parse_job_query(query_text, profile, feedback)
    if not criteria:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        return await wait_msg.edit_text(
            "📭 Нічого не знайшов за цим запитом. Спробуй ширші критерії."
        )

    await wait_msg.edit_text(f"✅ Знайдено {len(vacancies)} вакансій. Оцінюю відповідність...")

    scored = []
    for v in vacancies[:15]:
        v["_score"] = await jobs_service.score_vacancy(v, profile)
        scored.append(v)
    scored.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

    _results_cache[uid] = scored
    _criteria_cache[uid] = criteria
    _position_cache[uid] = 0

    top3 = "\n".join(
        f"{i+1}. {v.get('title','')} — Match {v['_score'].get('match_percent', '?')}%"
        for i, v in enumerate(scored[:3])
    )
    await msg.answer(f"🎯 *Найкращі для тебе:*\n\n{top3}")

    if not profile:
        await msg.answer(
            "💡 Заповни «👤 Мої дані для пошуку» — і я зможу оцінювати відповідність вакансій "
            "та писати персональні cover letter."
        )

    await _show_current_card(msg, uid)
    await msg.answer("Хочеш звузити пошук?", reply_markup=ikb_filters_menu())


async def _show_current_card(target: Message, uid: int):
    results = _results_cache.get(uid) or []
    pos = _position_cache.get(uid, 0)
    if pos >= len(results):
        return await target.answer("📭 Це всі знайдені вакансії. Спробуй новий запит або зміни фільтри.")

    v = results[pos]
    saved = await jobs_db.is_saved(uid, v["url"])
    text = _fmt_vacancy_card(v, len(results), pos)
    await target.answer(text, reply_markup=ikb_vacancy_card(pos, v["url"], saved=saved))


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


@router.message(F.text.regexp(r"^\s*шукаю\b", flags=re.IGNORECASE))
async def jobs_natural_shukau(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _run_search(msg, msg.from_user.id, msg.text.strip())


@router.message(F.text.regexp(r"знайди.*(вакансі|роботу)", flags=re.IGNORECASE))
async def jobs_natural_znaidy(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _run_search(msg, msg.from_user.id, msg.text.strip())


@router.callback_query(F.data == "jb_next")
async def jobs_next_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    _position_cache[uid] = _position_cache.get(uid, 0) + 1
    await cb.answer()
    await _show_current_card(cb.message, uid)


@router.callback_query(F.data.startswith("jb_save:"))
async def jobs_save_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    v = results[idx]
    if await jobs_db.is_saved(uid, v["url"]):
        return await cb.answer("Вже збережено ⭐")

    v["match_percent"] = v["_score"].get("match_percent")
    await jobs_db.save_vacancy(uid, v)
    await cb.answer("Збережено ⭐")


@router.callback_query(F.data.startswith("jb_notint:"))
async def jobs_not_interested_cb(cb: CallbackQuery):
    idx = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await cb.message.answer("Що саме не підійшло?", reply_markup=ikb_not_interested_reasons(idx))


@router.callback_query(F.data.startswith("jb_reason:"))
async def jobs_reason_cb(cb: CallbackQuery):
    _, idx_s, reason = cb.data.split(":", 2)
    idx = int(idx_s)
    uid = cb.from_user.id
    results = _results_cache.get(uid) or []
    if idx < len(results):
        await jobs_db.add_feedback(uid, results[idx], reason)
    await cb.answer("Врахую це наступного разу 👍")
    _position_cache[uid] = idx + 1
    await _show_current_card(cb.message, uid)


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
        InlineKeyboardButton(text="📋 Скопіювати", callback_data=f"jb_copy_noop"),
    ]])
    await cb.message.answer(f"✉️ *Cover Letter:*\n\n{letter}", reply_markup=kb)


@router.callback_query(F.data == "jb_copy_noop")
async def jobs_copy_noop_cb(cb: CallbackQuery):
    await cb.answer("Виділи текст вище і скопіюй ⬆️", show_alert=True)


@router.callback_query(F.data == "jbf_remote")
async def jobs_filter_remote_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    results = _results_cache.get(uid) or []
    filtered = jobs_service.apply_filters(results, {"remote_only": True})
    if not filtered:
        return await cb.answer("Нічого не знайдено з таким фільтром", show_alert=True)
    _results_cache[uid] = filtered
    _position_cache[uid] = 0
    await cb.answer(f"Залишилось {len(filtered)} вакансій")
    await _show_current_card(cb.message, uid)


@router.callback_query(F.data == "jbf_reset")
async def jobs_filter_reset_cb(cb: CallbackQuery):
    await cb.answer("Використай новий пошук, щоб скинути фільтри", show_alert=True)


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
        status = v.get("status", "saved")
        text = (
            f"⭐ *{v.get('title','')}*\n🏢 {v.get('company') or '—'}\n"
            f"📌 Статус: {status}\n🔗 {v.get('url','')}"
        )
        await msg.answer(text, reply_markup=ikb_saved_item(str(v["_id"]), status))


@router.callback_query(F.data.startswith("jb_status:"))
async def jobs_status_cb(cb: CallbackQuery):
    _, saved_id, new_status = cb.data.split(":", 2)
    ok = await jobs_db.update_status(saved_id, cb.from_user.id, new_status)
    if not ok:
        return await cb.answer("Не вдалося оновити", show_alert=True)
    await cb.answer(f"Статус: {new_status} ✅")
    await cb.message.edit_reply_markup(reply_markup=ikb_saved_item(saved_id, new_status))


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
        active = w.get("active", True)
        status_icon = "🔔" if active else "🔕"
        text = f"{status_icon} {criteria.get('profession','')} | {criteria.get('city') or 'будь-де'}"
        await msg.answer(text, reply_markup=ikb_watch_item(str(w["_id"]), active))


@router.callback_query(F.data.startswith("jbw_off:"))
async def jobs_watch_off_cb(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    await jobs_db.set_watch_active(wid, cb.from_user.id, False)
    await cb.answer("Моніторинг вимкнено 🔕")
    await cb.message.edit_reply_markup(reply_markup=ikb_watch_item(wid, False))


@router.callback_query(F.data.startswith("jbw_on:"))
async def jobs_watch_on_cb(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    await jobs_db.set_watch_active(wid, cb.from_user.id, True)
    await cb.answer("Моніторинг увімкнено 🔔")
    await cb.message.edit_reply_markup(reply_markup=ikb_watch_item(wid, True))


@router.callback_query(F.data.startswith("jbw_del:"))
async def jobs_watch_delete_cb(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    ok = await jobs_db.delete_watch(wid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)


@router.message(F.text == "📊 Мій пошук роботи")
async def jobs_stats(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    stats = await jobs_db.get_stats(msg.from_user.id)
    lines = [
        "📊 *Мій пошук роботи*", "",
        f"⭐ Збережено: {stats['total_saved']}",
        f"📨 Відгукнувся: {stats['applied']}",
        f"💬 Відповідей: {stats['response']}",
        f"🎤 Співбесід: {stats['interview']}",
        f"✅ Прийнято: {stats['hired']}",
        f"❌ Відмов: {stats['rejected']}",
    ]
    if stats["avg_match"] is not None:
        lines.append(f"\n🎯 Середній Match: {stats['avg_match']}%")
    if stats["top_titles"]:
        lines.append("\n📈 Найчастіші позиції:")
        lines += [f"• {t} ({c})" for t, c in stats["top_titles"]]
    if stats["top_companies"]:
        lines.append("\n🏢 Найчастіші компанії:")
        lines += [f"• {c} ({n})" for c, n in stats["top_companies"]]

    await msg.answer("\n".join(lines))