"""
ЗМІНЕНИЙ ФАЙЛ: handlers/jobs.py

НОВЕ (фільтр збігу + розширений профіль):
1. Ручний пошук (_run_search_inner): показуються лише вакансії, де
   match_percent СТРОГО БІЛЬШИЙ за config.settings.JOB_MIN_MATCH_PERCENT
   (за замовчуванням > 50%). Над списком — коротка статистика («оцінено N,
   підходять M»). Якщо жодна не пройшла поріг — окреме повідомлення з
   порадою і кнопкою «🔔 Зберегти пошук». Якщо оцінити неможливо (профіль
   порожній або AI недоступний) — показуємо все як раніше, з підказкою
   заповнити профіль, бо відфільтрувати нічим.
2. _profile_ready(): «є профіль» тепер означає «є хоча б одне заповнене
   поле», а не «існує документ у БД» (документ може містити лише службові
   поля, напр. дату підказки про профіль).
3. send_autosearch_digest: тексти підсумку згадують поріг збігу
   («підходящих вакансій», «нічого підходящого»).

Раніше додано (флоу "📨 Відгукнутися" з подвійним підтвердженням — БЕЗ ЗМІН):

🌙 Автопошук вакансій (майстер створення + вечірній дайджест):
1. AutosearchWizard — покроковий діалог створення автопошуку: назва →
   посада/ключові слова → місто → формат роботи (кнопки) → досвід
   (кнопки) → зарплата → підтвердження. Результат — jobs_db.
   add_search_watch(uid, criteria, seen_ids=[], title=name), тобто той
   самий механізм "watch", що вже використовувався для jb_watch/
   jb_watch_empty (без змін), просто тепер з людською назвою і
   структурованими критеріями замість вільного тексту.
2. jobs_watches_list (🔔 Мої монітори вакансій) — тепер показує title
   автопошуку (з фолбеком на criteria.profession для старих watch,
   створених через jb_watch), і додає кнопку "➕ Створити автопошук".
3. send_autosearch_digest(bot, uid, searches) — ПУБЛІЧНА функція,
   викликається планувальником (scheduler/jobs_watch_jobs.py) увечері.
   Формує текстовий підсумок по кожному активному автопошуку і пушить
   усі знайдені за день вакансії в _results_cache[uid], надсилаючи їх
   ТИМИ Ж інтерактивними картками (Зберегти/AI аналіз/📨 Відгукнутися),
   що й ручний пошук — тому "📨 Відгукнутися" одразу працює і з карток
   дайджесту, без дублювання логіки флоу відгуку.
"""

import logging
import re

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from config.settings import JOB_MIN_MATCH_PERCENT
from database import job_profile as job_profile_db
from database import jobs as jobs_db
from services import jobs_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.jobs import (
    ikb_vacancy_card, ikb_not_interested_reasons, ikb_filters_menu,
    ikb_saved_item, ikb_watch_item, ikb_empty_search,
    ikb_apply_review, ikb_apply_final_confirm, ikb_apply_manual,
    ikb_autosearch_list_header, ikb_autosearch_remote, ikb_autosearch_level,
    ikb_autosearch_confirm,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="jobs")

_results_cache: dict[int, list[dict]] = {}
_criteria_cache: dict[int, dict] = {}
_position_cache: dict[int, int] = {}

_apply_cache: dict[int, dict] = {}


class JobSearch(StatesGroup):
    waiting_query = State()


class JobApply(StatesGroup):
    editing_cover_letter = State()


class AutosearchWizard(StatesGroup):
    waiting_name = State()
    waiting_position = State()
    waiting_city = State()
    waiting_remote = State()
    waiting_level = State()
    waiting_salary = State()


_WORK_FORMAT_LABELS = {"remote": "Remote", "office": "Офіс", "hybrid": "Гібрид"}

_MD_SPECIAL_RE = re.compile(r"([_*`\[])")


def _md_escape(value) -> str:
    if not value:
        return ""
    return _MD_SPECIAL_RE.sub(r"\\\1", str(value))


def _profile_ready(profile: dict | None) -> bool:
    """True, якщо в профілі є хоча б одне заповнене поле. Сам по собі
    документ у БД ще не означає профіль: він може містити лише службові
    поля (дата підказки про профіль, digest_seen_ids тощо)."""
    return bool(job_profile_db.format_profile_for_ai(profile))


def _fmt_match_line(score: dict) -> str:
    mp = score.get("match_percent")
    return f"Match {mp}%" if mp is not None else "Match: н/д (заповни профіль)"


async def _answer_safe(target: Message, text: str, **kwargs) -> Message:
    try:
        return await target.answer(text, **kwargs)
    except TelegramBadRequest:
        logger.warning("Markdown parse failed for message, retrying without formatting")
        kwargs.pop("parse_mode", None)
        return await target.answer(text, parse_mode=None, **kwargs)


def _fmt_vacancy_card(v: dict, total_shown: int, position: int) -> str:
    score = v.get("_score", {})
    lines = [f"💼 *{_md_escape(v.get('title',''))}*", ""]
    lines.append(f"🏢 {_md_escape(v.get('company')) or '—'}")
    if v.get("salary"):
        lines.append(f"💰 {_md_escape(v['salary'])}")

    format_label = _WORK_FORMAT_LABELS.get(v.get("work_format"))
    loc_bits = [x for x in [_md_escape(v.get("location")), format_label] if x]
    if loc_bits:
        lines.append(f"📍 {' / '.join(loc_bits)}")

    if v.get("experience"):
        lines.append(f"📊 {_md_escape(v['experience'])}")

    if score.get("match_percent") is not None:
        lines.append(f"\n🎯 Match: *{score['match_percent']}%*\n")
        for tag in (score.get("fits") or [])[:4]:
            lines.append(f"🟢 {_md_escape(tag)}")
        for tag in (score.get("missing") or [])[:2]:
            lines.append(f"🟡 {_md_escape(tag)}")

    sources = v.get("sources") or [v.get("source", "")]
    lines.append(f"\n🌐 Джерело: {_md_escape(' · '.join(s for s in sources if s))}")
    lines.append(f"🔗 {v.get('url','')}")

    if score.get("advice"):
        lines.append(f"\n💡 {_md_escape(score['advice'])}")

    lines.append(f"\n_{position + 1} з {total_shown}_")
    return "\n".join(lines)


async def _safe_edit(target: Message, text: str, **kwargs) -> Message:
    try:
        return await target.edit_text(text, **kwargs)
    except TelegramBadRequest:
        logger.warning("edit_text failed for message_id=%s, sending new message instead", target.message_id)
        return await target.answer(text, **kwargs)


async def _run_search(msg: Message, uid: int, query_text: str):
    try:
        await _run_search_inner(msg, uid, query_text)
    except Exception:
        logger.exception("Job search crashed for uid=%s query=%r", uid, query_text)
        try:
            await msg.answer(
                "⚠️ Сталася помилка під час пошуку вакансій. Спробуй ще раз трохи пізніше.",
                reply_markup=kb_main(),
            )
        except Exception:
            logger.exception("Failed to notify uid=%s about job search crash", uid)


async def _run_search_inner(msg: Message, uid: int, query_text: str):
    profile = await job_profile_db.get_profile(uid)
    feedback = await jobs_db.get_recent_feedback(uid)

    wait_msg = await msg.answer("🔎 Аналізую запит і шукаю вакансії...", reply_markup=kb_main())
    criteria = await jobs_service.parse_job_query(query_text, profile, feedback)
    if not criteria:
        return await _safe_edit(wait_msg, AI_ERROR_TEXT)

    _criteria_cache[uid] = criteria

    vacancies = await jobs_service.search_vacancies(criteria)
    if not vacancies:
        _results_cache[uid] = []
        _position_cache[uid] = 0
        return await _safe_edit(
            wait_msg,
            "📭 Нічого не знайшов за цим запитом на Djinni / Work.ua / Robota.ua.\n\n"
            "Спробуй ширші критерії (менше уточнень одразу) — або збережи цей пошук, "
            "і я сам повідомлю, щойно з'явиться щось підходяще.",
            reply_markup=ikb_empty_search(),
        )

    await _safe_edit(wait_msg, f"✅ Знайдено {len(vacancies)} вакансій. Оцінюю відповідність...")

    scored = []
    for v in vacancies[:15]:
        v["_score"] = await jobs_service.score_vacancy(v, profile)
        scored.append(v)

    # Відфільтрувати за збігом можна лише коли є чим оцінювати: заповнений
    # профіль і робочий AI. Інакше показуємо все, як раніше (з підказкою).
    scoring_active = _profile_ready(profile) and jobs_service.ai_service.is_available()
    if scoring_active:
        matched = jobs_service.filter_by_min_match(scored)
        unscored = sum(1 for v in scored if jobs_service.get_match_percent(v) is None)
    else:
        matched = scored
        unscored = 0
    matched.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

    if not matched:
        _results_cache[uid] = []
        _position_cache[uid] = 0
        if unscored == len(scored):
            text = (
                f"⚠️ Знайшов {len(scored)} вакансій, але не вдалося оцінити їх відповідність "
                "профілю. Спробуй ще раз трохи пізніше."
            )
        else:
            text = (
                f"📭 Знайшов {len(scored)} вакансій, але жодна не підходить більш ніж на "
                f"{JOB_MIN_MATCH_PERCENT}% з твоїм профілем"
                + (f" (ще {unscored} не вдалося оцінити)" if unscored else "")
                + ".\n\nСпробуй ширший запит, доповни «👤 Мої дані для пошуку» — "
                "або збережи цей пошук, і я сам повідомлю, коли з'явиться щось підходяще."
            )
        return await _safe_edit(wait_msg, text, reply_markup=ikb_empty_search())

    _results_cache[uid] = matched
    _position_cache[uid] = 0

    top3 = "\n\n".join(
        f"{i+1}. {_md_escape(v.get('title',''))} — {_fmt_match_line(v['_score'])}\n🔗 {v.get('url','')}"
        for i, v in enumerate(matched[:3])
    )
    header = "🎯 *Найкращі для тебе:*"
    if scoring_active:
        note = f"Оцінено {len(scored)}, підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%: {len(matched)}"
        if unscored:
            note += f" (ще {unscored} не вдалося оцінити)"
        header += f"\n_{note}_"
    await _answer_safe(msg, f"{header}\n\n{top3}")

    if not _profile_ready(profile):
        await msg.answer(
            "💡 Заповни «👤 Мої дані для пошуку» — і я зможу оцінювати відповідність вакансій, "
            f"показувати лише ті, що підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%, "
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
    await _answer_safe(target, text, reply_markup=ikb_vacancy_card(pos, v["url"], saved=saved))


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


@router.message(StateFilter(None), F.text == "❌ Скасувати")
async def jobs_stray_cancel(msg: Message, state: FSMContext):
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


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


@router.callback_query(F.data.startswith("jb_analyze:"))
async def jobs_analyze_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    if not jobs_service.ai_service.is_available():
        await cb.answer()
        return await cb.message.answer(AI_ERROR_TEXT)

    await cb.answer("Аналізую вакансію...")
    wait_msg = await cb.message.answer("🤖 Готую детальний AI-аналіз вакансії (вимоги, навички, рівень)...")

    try:
        profile = await job_profile_db.get_profile(uid)
        v = results[idx]
        analysis = await jobs_service.analyze_vacancy_full(v, profile)
        if not analysis:
            return await _safe_edit(wait_msg, AI_ERROR_TEXT)

        await _safe_edit(
            wait_msg,
            f"🤖 *AI аналіз вакансії*\n*{_md_escape(v.get('title',''))}*\n\n{_md_escape(analysis)}",
        )
    except Exception:
        logger.exception("Vacancy analysis crashed for uid=%s idx=%s", uid, idx)
        await _safe_edit(wait_msg, AI_ERROR_TEXT)


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
    """Стара, легка дія — просто показати Cover Letter із кнопкою
    копіювання, без флоу відправки. Залишена БЕЗ ЗМІН — нова кнопка
    "📨 Відгукнутися" (jb_apply_start) це не замінює, а доповнює."""
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    profile = await job_profile_db.get_profile(uid)
    if not _profile_ready(profile):
        await cb.answer()
        return await cb.message.answer(
            "⚠️ Спочатку заповни «👤 Мої дані для пошуку» — cover letter пишеться на основі цих даних."
        )

    await cb.answer("Пишу cover letter...")
    v = results[idx]

    try:
        letter = await jobs_service.generate_cover_letter(v, profile)
    except Exception:
        logger.exception("Cover letter generation crashed for uid=%s idx=%s", uid, idx)
        letter = None

    if not letter:
        return await cb.message.answer(AI_ERROR_TEXT)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Перегенерувати", callback_data=f"jb_cover:{idx}"),
        InlineKeyboardButton(text="📋 Скопіювати", callback_data=f"jb_copy_noop"),
    ]])
    await _answer_safe(cb.message, f"✉️ *Cover Letter:*\n\n{_md_escape(letter)}", reply_markup=kb)


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


@router.callback_query(F.data == "jb_watch_empty")
async def jobs_watch_empty_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    criteria = _criteria_cache.get(uid)
    if not criteria:
        return await cb.answer("Спочатку зроби пошук.", show_alert=True)

    await jobs_db.add_search_watch(uid, criteria, [])
    await cb.answer(
        "Додано до моніторингу 🔔 Повідомлю, щойно з'являться нові вакансії за цим запитом.",
        show_alert=True,
    )


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
            f"⭐ *{_md_escape(v.get('title',''))}*\n🏢 {_md_escape(v.get('company')) or '—'}\n"
            f"📌 Статус: {status}\n🔗 {v.get('url','')}"
        )
        await _answer_safe(msg, text, reply_markup=ikb_saved_item(str(v["_id"]), status))


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


# =========================================================
# 🔔 Мої монітори вакансій → 🌙 Мої автопошуки (розширено)
# =========================================================

@router.message(F.text == "🔔 Мої монітори вакансій")
async def jobs_watches_list(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    watches = await jobs_db.get_user_watches(msg.from_user.id)

    intro = (
        "🔔 *Мої автопошуки вакансій*\n\n"
        "Кожен автопошук самостійно шукає вакансії ДВІЧІ на день (в обід і "
        "ввечері) і надсилає підсумок увечері."
        if watches else
        "📭 Ще немає жодного автопошуку.\n\n"
        "Створи перший — і я сам шукатиму для тебе двічі на день і "
        "пришлю підсумок увечері."
    )
    await msg.answer(intro, reply_markup=ikb_autosearch_list_header())

    for w in watches:
        title = w.get("title") or w.get("criteria", {}).get("profession") or "Без назви"
        active = w.get("active", True)
        status_icon = "🔔" if active else "🔕"
        criteria = w.get("criteria", {})
        subtitle_bits = [x for x in [criteria.get("city"), criteria.get("work_format")] if x]
        subtitle = f" ({', '.join(subtitle_bits)})" if subtitle_bits else ""
        text = f"{status_icon} *{_md_escape(title)}*{_md_escape(subtitle)}"
        await _answer_safe(msg, text, reply_markup=ikb_watch_item(str(w["_id"]), active))


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
        lines += [f"• {_md_escape(t)} ({c})" for t, c in stats["top_titles"]]
    if stats["top_companies"]:
        lines.append("\n🏢 Найчастіші компанії:")
        lines += [f"• {_md_escape(c)} ({n})" for c, n in stats["top_companies"]]

    await _answer_safe(msg, "\n".join(lines))


# =========================================================
# 📨 Відгукнутися — флоу з двома підтвердженнями (БЕЗ ЗМІН)
# =========================================================

def _fmt_apply_review(vacancy: dict, cover_letter: str) -> str:
    score = vacancy.get("_score") or {}
    lines = [
        f"💼 *{_md_escape(vacancy.get('title',''))}*",
        f"🏢 {_md_escape(vacancy.get('company')) or '—'}",
    ]
    if score.get("match_percent") is not None:
        lines.append(f"🎯 Match: *{score['match_percent']}%*")
    if score.get("advice"):
        lines.append(f"\n🤖 *Короткий аналіз:* {_md_escape(score['advice'])}")
    fits = score.get("fits") or []
    if fits:
        lines.append(f"🟢 Підходить: {_md_escape(', '.join(fits[:4]))}")
    lines.append(f"\n✉️ *Cover Letter:*\n{_md_escape(cover_letter)}")
    return "\n".join(lines)


async def _show_apply_review(target: Message, idx: int, vacancy: dict, cover_letter: str) -> None:
    await _answer_safe(
        target, _fmt_apply_review(vacancy, cover_letter), reply_markup=ikb_apply_review(idx)
    )


@router.callback_query(F.data.startswith("jb_apply_start:"))
async def jobs_apply_start_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    profile = await job_profile_db.get_profile(uid)
    if not _profile_ready(profile):
        await cb.answer()
        return await cb.message.answer(
            "⚠️ Спочатку заповни «👤 Мої дані для пошуку» — без цього я не зможу підготувати "
            "персональний Cover Letter для відгуку."
        )

    if not jobs_service.ai_service.is_available():
        await cb.answer()
        return await cb.message.answer(AI_ERROR_TEXT)

    await cb.answer()
    wait = await cb.message.answer("🤖 Готую Cover Letter і аналіз вакансії перед відгуком...")

    v = results[idx]
    try:
        if v.get("_score") is None:
            v["_score"] = await jobs_service.score_vacancy(v, profile)
        letter = await jobs_service.generate_cover_letter(v, profile)
    except Exception:
        logger.exception("Apply-flow prep crashed for uid=%s idx=%s", uid, idx)
        letter = None

    if not letter:
        return await _safe_edit(wait, AI_ERROR_TEXT)

    _apply_cache[uid] = {"idx": idx, "vacancy": v, "cover_letter": letter}

    try:
        await wait.delete()
    except Exception:
        pass
    await _show_apply_review(cb.message, idx, v, letter)


@router.callback_query(F.data.startswith("jb_regen_cover:"))
async def jobs_apply_regen_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    profile = await job_profile_db.get_profile(uid)
    await cb.answer("Перегенеровую...")

    try:
        letter = await jobs_service.generate_cover_letter(apply_state["vacancy"], profile)
    except Exception:
        logger.exception("Cover letter regen crashed for uid=%s idx=%s", uid, idx)
        letter = None

    if not letter:
        return await cb.message.answer(AI_ERROR_TEXT)

    apply_state["cover_letter"] = letter
    await _show_apply_review(cb.message, idx, apply_state["vacancy"], letter)


@router.callback_query(F.data.startswith("jb_edit_cover:"))
async def jobs_apply_edit_start_cb(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    await cb.answer()
    await state.set_state(JobApply.editing_cover_letter)
    await state.update_data(apply_idx=idx)
    await cb.message.answer(
        "✏️ Надішли новий текст Cover Letter (повністю, він замінить поточний):",
        reply_markup=kb_cancel(),
    )


@router.message(JobApply.editing_cover_letter, F.text == "❌ Скасувати")
async def jobs_apply_edit_cancel(msg: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("apply_idx")
    await state.clear()
    uid = msg.from_user.id
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await msg.answer("Скасовано, залишаю попередній текст.")
    await _show_apply_review(msg, idx, apply_state["vacancy"], apply_state["cover_letter"])


@router.message(JobApply.editing_cover_letter)
async def jobs_apply_edit_apply(msg: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("apply_idx")
    await state.clear()
    uid = msg.from_user.id

    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    new_text = (msg.text or "").strip()
    if not new_text:
        await state.set_state(JobApply.editing_cover_letter)
        await state.update_data(apply_idx=idx)
        return await msg.answer("⚠️ Текст не може бути порожнім. Надішли текст Cover Letter:")

    apply_state["cover_letter"] = new_text
    await _show_apply_review(msg, idx, apply_state["vacancy"], new_text)


@router.callback_query(F.data.startswith("jb_send_confirm:"))
async def jobs_apply_send_confirm_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    v = apply_state["vacancy"]
    await cb.answer()
    await cb.message.answer(
        "❓ *Готово до відправлення?*\n\n"
        f"Вакансія: {_md_escape(v.get('title',''))}\n"
        f"Компанія: {_md_escape(v.get('company')) or '—'}\n\n"
        "Відправити заявку?",
        reply_markup=ikb_apply_final_confirm(idx),
    )


@router.callback_query(F.data.startswith("jb_send_final:"))
async def jobs_apply_send_final_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    v = apply_state["vacancy"]
    letter = apply_state["cover_letter"]
    await cb.answer()
    wait = await cb.message.answer("⏳ Перевіряю можливість автоматичної подачі...")

    try:
        result = await jobs_service.attempt_auto_apply(v, letter)
    except Exception:
        logger.exception("attempt_auto_apply crashed for uid=%s idx=%s", uid, idx)
        result = {"success": False, "reason": "error"}

    if result.get("success"):
        await jobs_db.mark_applied(uid, v)
        _apply_cache.pop(uid, None)
        return await _safe_edit(
            wait,
            f"✅ Заявку відправлено автоматично!\n💼 {_md_escape(v.get('title',''))}",
        )

    await _safe_edit(
        wait,
        "ℹ️ Автоматична подача заявки на цьому сайті наразі технічно неможлива "
        "(немає публічного API / форма захищена від ботів).\n\n"
        "Відкрий вакансію за посиланням нижче, скопіюй Cover Letter і надішли "
        "заявку вручну. Коли зробиш це — натисни «Я відгукнувся вручну».",
    )
    await cb.message.answer(
        f"✉️ *Cover Letter для копіювання:*\n\n{_md_escape(letter)}",
        reply_markup=ikb_apply_manual(idx, v.get("url", "")),
    )


@router.callback_query(F.data.startswith("jb_mark_applied:"))
async def jobs_apply_mark_manual_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    apply_state = _apply_cache.get(uid)
    if not apply_state or apply_state["idx"] != idx:
        return await cb.answer("Сесія застаріла", show_alert=True)

    await jobs_db.mark_applied(uid, apply_state["vacancy"])
    _apply_cache.pop(uid, None)
    await cb.answer("Позначено як 'Відгукнувся' ✅", show_alert=True)


@router.callback_query(F.data.startswith("jb_apply_cancel:"))
async def jobs_apply_cancel_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    _apply_cache.pop(uid, None)
    await cb.answer("Скасовано")
    try:
        await cb.message.delete()
    except Exception:
        pass


# =========================================================
# 🌙 Автопошук — майстер створення
# =========================================================

@router.callback_query(F.data == "jb_autosearch_new")
async def autosearch_new_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AutosearchWizard.waiting_name)
    await cb.message.answer(
        "➕ *Новий автопошук*\n\n"
        "Придумай коротку назву (наприклад: «Frontend Developer», "
        "«Remote / Online робота», «Junior Developer», «Робота в Польщі»):",
        reply_markup=kb_cancel(),
    )


@router.message(AutosearchWizard.waiting_name, F.text == "❌ Скасувати")
@router.message(AutosearchWizard.waiting_position, F.text == "❌ Скасувати")
@router.message(AutosearchWizard.waiting_city, F.text == "❌ Скасувати")
@router.message(AutosearchWizard.waiting_salary, F.text == "❌ Скасувати")
async def autosearch_wizard_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(AutosearchWizard.waiting_name)
async def autosearch_name_received(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()[:80]
    if not name:
        return await msg.answer("⚠️ Назва не може бути порожньою. Спробуй ще раз:")
    await state.update_data(name=name)
    await state.set_state(AutosearchWizard.waiting_position)
    await msg.answer(
        "Яку посаду шукати? Можна кілька через кому.\n"
        "Наприклад: «Frontend Developer, React, Junior» або «Водій категорії B»:"
    )


@router.message(AutosearchWizard.waiting_position)
async def autosearch_position_received(msg: Message, state: FSMContext):
    position = (msg.text or "").strip()[:200]
    if not position:
        return await msg.answer("⚠️ Напиши, будь ласка, посаду або ключові слова:")
    await state.update_data(position=position)
    await state.set_state(AutosearchWizard.waiting_city)
    await msg.answer("Місто чи країна? Напиши «будь-де», якщо не важливо:")


@router.message(AutosearchWizard.waiting_city)
async def autosearch_city_received(msg: Message, state: FSMContext):
    await state.update_data(city=(msg.text or "").strip()[:100])
    await state.set_state(AutosearchWizard.waiting_remote)
    await msg.answer("Формат роботи?", reply_markup=ikb_autosearch_remote())


@router.message(AutosearchWizard.waiting_remote)
@router.message(AutosearchWizard.waiting_level)
async def autosearch_wizard_need_buttons(msg: Message):
    await msg.answer("Будь ласка, обери варіант кнопкою вище ⬆️")


@router.callback_query(AutosearchWizard.waiting_remote, F.data.startswith("aws_remote:"))
async def autosearch_remote_cb(cb: CallbackQuery, state: FSMContext):
    value = cb.data.split(":", 1)[1]
    await state.update_data(work_format=value)
    await state.set_state(AutosearchWizard.waiting_level)
    await cb.answer()
    await cb.message.answer("Рівень досвіду?", reply_markup=ikb_autosearch_level())


@router.callback_query(AutosearchWizard.waiting_level, F.data.startswith("aws_level:"))
async def autosearch_level_cb(cb: CallbackQuery, state: FSMContext):
    value = cb.data.split(":", 1)[1]
    await state.update_data(level=value)
    await state.set_state(AutosearchWizard.waiting_salary)
    await cb.answer()
    await cb.message.answer(
        "Мінімальна бажана зарплата? Напиши число (напр. 30000 або 1500$) "
        "або «пропустити»:",
        reply_markup=kb_cancel(),
    )


@router.message(AutosearchWizard.waiting_salary)
async def autosearch_salary_received(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    salary_min = None
    salary_currency = "UAH"
    if raw.lower() not in ("пропустити", "skip", "-", ""):
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            salary_min = int(digits)
        salary_currency = "USD" if "$" in raw else "UAH"

    await state.update_data(salary_min=salary_min, salary_currency=salary_currency)
    data = await state.get_data()

    criteria = jobs_service.build_criteria_from_wizard(data)
    await state.update_data(criteria=criteria)
    await state.set_state(None)  # далі чекаємо лише callback-підтвердження (aws_confirm/aws_cancel)

    level_labels = {"no_exp": "без досвіду", "junior": "Junior", "middle": "Middle", "senior": "Senior", "any": "будь-який"}
    format_labels = {"remote": "Remote", "office": "Офіс", "hybrid": "Гібрид", "any": "не важливо"}
    summary = (
        f"📋 *Перевір автопошук:*\n\n"
        f"📝 Назва: {_md_escape(data['name'])}\n"
        f"💼 Посада/ключові слова: {_md_escape(data['position'])}\n"
        f"📍 Локація: {_md_escape(data.get('city') or 'будь-де')}\n"
        f"💻 Формат: {format_labels.get(data.get('work_format', 'any'), 'не важливо')}\n"
        f"📊 Досвід: {level_labels.get(data.get('level', 'any'), 'будь-який')}\n"
        f"💰 Зарплата від: {salary_min if salary_min else 'не вказано'} {salary_currency if salary_min else ''}\n\n"
        "Автопошук працюватиме двічі на день і надсилатиме підсумок увечері "
        f"(лише вакансії, що підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%)."
    )
    await _answer_safe(msg, summary, reply_markup=ikb_autosearch_confirm())


@router.callback_query(F.data == "aws_confirm")
async def autosearch_confirm_cb(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    criteria = data.get("criteria")
    name = data.get("name")
    if not criteria or not name:
        await state.clear()
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    await jobs_db.add_search_watch(cb.from_user.id, criteria, seen_ids=[], title=name)
    await state.clear()
    await cb.answer()
    await cb.message.answer(
        f"✅ Автопошук «{_md_escape(name)}» створено! Шукатиму двічі на день "
        "і надсилатиму підсумок увечері.",
        reply_markup=kb_main(),
    )


@router.callback_query(F.data == "aws_cancel")
async def autosearch_cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_main())


# =========================================================
# 🌙 Вечірній дайджест — виклик з планувальника
# =========================================================

async def send_autosearch_digest(bot: Bot, uid: int, searches: list[dict]) -> None:
    """Викликається ЛИШЕ scheduler/jobs_watch_jobs.py (run_autosearches_evening)
    раз на день. searches: [{"title": str, "vacancies": [vacancy_with_score, ...]}, ...]
    — по одному запису на кожен активний автопошук користувача. Планувальник
    уже відфільтрував вакансії за порогом збігу (> JOB_MIN_MATCH_PERCENT).

    Надсилає текстовий підсумок по кожному автопошуку, а потім — ТІ Ж САМІ
    інтерактивні картки (Зберегти/AI аналіз/📨 Відгукнутися/❌ Не показувати
    такі), що й у ручному пошуку, тому кнопка "📨 Відгукнутися" з дайджесту
    одразу веде в уже готовий флоу з подвійним підтвердженням, без дублювання
    логіки. Гарантовано показує хоча б одну вакансію повною карткою, якщо
    хоч один автопошук щось знайшов за день."""
    lines = [
        "🌙 *Вечірній підсумок автопошуку вакансій*",
        f"_Лише вакансії, що підходять більш ніж на {JOB_MIN_MATCH_PERCENT}%_",
        "",
    ]
    all_vacancies: list[dict] = []
    seen_urls: set[str] = set()
    any_found = False

    for entry in searches:
        title = entry["title"]
        vacancies = entry["vacancies"]
        if vacancies:
            any_found = True
            lines.append(f"🔍 *{_md_escape(title)}* — підходящих вакансій: {len(vacancies)}")
            for v in vacancies:
                if v["url"] not in seen_urls:
                    seen_urls.add(v["url"])
                    all_vacancies.append(v)
        else:
            lines.append(f"🔍 *{_md_escape(title)}* — нічого підходящого сьогодні")

    if not any_found:
        lines.append(
            f"\nСьогодні по жодному з твоїх автопошуків не знайшлось вакансій зі збігом "
            f"понад {JOB_MIN_MATCH_PERCENT}% 😔"
        )

    text = "\n".join(lines)
    try:
        await bot.send_message(uid, text)
    except TelegramBadRequest:
        await bot.send_message(uid, text, parse_mode=None)

    if not all_vacancies:
        return

    all_vacancies.sort(key=lambda v: v["_score"].get("match_percent") or 0, reverse=True)

    _results_cache[uid] = all_vacancies
    _position_cache[uid] = 0

    for i, v in enumerate(all_vacancies):
        saved = await jobs_db.is_saved(uid, v["url"])
        card_text = _fmt_vacancy_card(v, len(all_vacancies), i)
        kb = ikb_vacancy_card(i, v["url"], saved=saved)
        try:
            await bot.send_message(uid, card_text, reply_markup=kb)
        except TelegramBadRequest:
            await bot.send_message(uid, card_text, reply_markup=kb, parse_mode=None)