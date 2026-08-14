import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import LABELS, CATEGORIES, DB_ERROR_TEXT, AI_ERROR_TEXT, AI_LIMIT_TEXT, STATUS_PENDING
from config.settings import AI_DAILY_LIMIT
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import users as users_db
from database import ai_usage as ai_usage_db
from services import ai_service
from services import planner_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.ai import ikb_ai_menu, ikb_ai_plan_preview, ikb_ai_settings, ikb_ai_usage
from handlers.common import require_auth, ai_suggestions_cache, compute_daily_stats

logger = logging.getLogger("tasks_bot")
router = Router(name="ai_planner")


class AiSettings(StatesGroup):
    setting_time = State()


def _fmt_plan_preview(plan: dict, selected: set) -> str:
    tasks = plan.get("tasks", [])
    total_minutes = sum(t.get("estimated_minutes", 30) for i, t in enumerate(tasks) if i in selected)
    lines = ["☀️ *AI План на сьогодні*", ""]
    if plan.get("focus"):
        lines.append(f"🎯 Головний фокус: *{plan['focus']}*")
    if plan.get("reason"):
        lines.append(f"_{plan['reason']}_")
    if plan.get("advice"):
        lines.append(f"💡 Порада: {plan['advice']}")
    lines.append("")
    lines.append("🔥 *Пріоритети:*")
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t["label"], {})
        cat = CATEGORIES.get(t["category"], {})
        lines.append(
            f"{mark} {label.get('emoji','')} *{t['time']}* — {t['text']} "
            f"({cat.get('emoji','')} {cat.get('name','')}, ~{t.get('estimated_minutes', 30)} хв)"
        )
    h, m = divmod(total_minutes, 60)
    load_parts = ([f"{h} год"] if h else []) + ([f"{m} хв"] if m else [])
    lines.append("")
    lines.append(f"📊 Заплановане навантаження: ~{' '.join(load_parts) or '0 хв'}")
    lines.append("")
    lines.append("Натисни на задачу, щоб зняти/додати позначку, потім підтверди.")
    return "\n".join(lines)


@router.message(F.text == "🤖 AI Планер")
async def ai_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(
            "⚠️ AI Планер не налаштований.\nЗадай `AI_API_KEY` (або `OPENAI_API_KEY`) у змінних середовища, щоб увімкнути цю функцію.",
            reply_markup=kb_main(),
        )
    await msg.answer("🤖 *AI Планер*\n\nЩо зробити?", reply_markup=ikb_ai_menu())


@router.callback_query(F.data == "ai_close")
async def ai_close_cb(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await cb.answer()


@router.callback_query(F.data == "ai_menu_back")
async def ai_menu_back_cb(cb: CallbackQuery):
    await cb.message.edit_text("🤖 *AI Планер*\n\nЩо зробити?", reply_markup=ikb_ai_menu())
    await cb.answer()


async def _generate_and_show_plan(cb: CallbackQuery):
    uid = cb.from_user.id
    allowed, remaining = await planner_service.check_ai_limit(uid)
    if not allowed:
        return await cb.message.edit_text(AI_LIMIT_TEXT)

    await cb.message.edit_text("☀️ Аналізую твої задачі, цілі, проєкти та фінанси, зачекай кілька секунд...")
    plan = await planner_service.generate_daily_plan(uid)
    if not plan or not plan.get("tasks"):
        return await cb.message.edit_text(AI_ERROR_TEXT)
    selected = set(range(len(plan["tasks"])))
    ai_suggestions_cache[uid] = {"plan": plan, "selected": selected}
    await cb.message.edit_text(
        _fmt_plan_preview(plan, selected),
        reply_markup=ikb_ai_plan_preview(plan["tasks"], selected),
    )


@router.callback_query(F.data == "ai_plan")
async def ai_plan_cb(cb: CallbackQuery):
    try:
        await cb.answer("Генерую...")
        await _generate_and_show_plan(cb)
    except Exception:
        logger.exception("ai_plan_cb failed")
        await _safe_edit(cb, AI_ERROR_TEXT)


@router.callback_query(F.data == "ai_regenerate")
async def ai_regenerate_cb(cb: CallbackQuery):
    try:
        await cb.answer("Перегенеровую...")
        await _generate_and_show_plan(cb)
    except Exception:
        logger.exception("ai_regenerate_cb failed")
        await _safe_edit(cb, AI_ERROR_TEXT)


@router.callback_query(F.data == "ai_current_plan")
async def ai_current_plan_cb(cb: CallbackQuery):
    data = ai_suggestions_cache.get(cb.from_user.id)
    if not data:
        await cb.answer("Плану ще немає. Натисни «🚀 Створити план на сьогодні».", show_alert=True)
        return
    await cb.message.edit_text(
        _fmt_plan_preview(data["plan"], data["selected"]),
        reply_markup=ikb_ai_plan_preview(data["plan"]["tasks"], data["selected"]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("aiptoggle:"))
async def aiptoggle_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = ai_suggestions_cache.get(uid)
    if not data:
        return await cb.answer("Сесія застаріла, згенеруй план заново.", show_alert=True)
    idx = int(cb.data.split(":")[1])
    sel = data["selected"]
    if idx in sel:
        sel.discard(idx)
    else:
        sel.add(idx)
    plan = data["plan"]
    await cb.message.edit_text(_fmt_plan_preview(plan, sel), reply_markup=ikb_ai_plan_preview(plan["tasks"], sel))
    await cb.answer()


@router.callback_query(F.data == "aip_select_all")
async def aip_select_all_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = ai_suggestions_cache.get(uid)
    if not data:
        return await cb.answer("Сесія застаріла, згенеруй план заново.", show_alert=True)
    data["selected"] = set(range(len(data["plan"]["tasks"])))
    plan = data["plan"]
    await cb.message.edit_text(_fmt_plan_preview(plan, data["selected"]), reply_markup=ikb_ai_plan_preview(plan["tasks"], data["selected"]))
    await cb.answer()


@router.callback_query(F.data == "aip_add")
async def aip_add_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = ai_suggestions_cache.pop(uid, None)
    if not data or not data["selected"]:
        return await cb.answer("Нічого не обрано.", show_alert=True)
    tasks = data["plan"]["tasks"]
    today = datetime.now().strftime("%d.%m.%Y")
    added = 0
    try:
        for i in sorted(data["selected"]):
            it = tasks[i]
            new_id = await tasks_db.next_task_id()
            task = {
                "id": new_id,
                "uid": uid,
                "text": it["text"],
                "label": it["label"],
                "category": it["category"],
                "due": f"{today} {it['time']}",
                "status": STATUS_PENDING,
                "pinned": False,
                "subtasks": [],
                "created_at": datetime.now().isoformat(),
                "completed_at": None,
                "reminded_before": False,
                "missed_flagged": False,
                "missed_counted": False,
                "postponed_count": 0,
                "postponed_today": False,
                "source": "ai",
                "project_id": None,
                "estimated_minutes": it.get("estimated_minutes"),
            }
            await tasks_db.add_task(task)
            added += 1
    except DBUnavailable:
        pass
    await cb.message.edit_text(
        f"✅ *Створено план!*\n\nДодано {added} задач(і) — вони вже в твоєму списку активних, "
        f"з нагадуваннями та XP як завжди."
    )
    await cb.answer()


@router.callback_query(F.data == "aip_cancel")
async def aip_cancel_cb(cb: CallbackQuery):
    ai_suggestions_cache.pop(cb.from_user.id, None)
    await cb.message.edit_text("❌ План відхилено. Задачі не додано.")
    await cb.answer()


@router.callback_query(F.data == "ai_analysis")
async def ai_analysis_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        await cb.answer("Аналізую...")
        allowed, _ = await planner_service.check_ai_limit(uid)
        if not allowed:
            return await cb.message.edit_text(AI_LIMIT_TEXT)
        await cb.message.edit_text("📊 Аналізую твою продуктивність...")
        stats = await compute_daily_stats(uid, tasks_db)
        text = await planner_service.generate_daily_analysis(uid, stats)
        if not text:
            return await cb.message.edit_text(AI_ERROR_TEXT)
        await cb.message.edit_text(f"📊 *Аналіз продуктивності*\n\n{text}")
    except Exception:
        logger.exception("ai_analysis_cb failed")
        await _safe_edit(cb, AI_ERROR_TEXT)


# =========================================================
# НАЛАШТУВАННЯ AI ПЛАНЕРА
# =========================================================

@router.callback_query(F.data == "ai_settings")
async def ai_settings_cb(cb: CallbackQuery):
    st = await users_db.get_user_state(cb.from_user.id)
    enabled = st.get("ai_morning_enabled", True)
    await cb.message.edit_text("⚙️ *Налаштування AI Планера*", reply_markup=ikb_ai_settings(enabled))
    await cb.answer()


@router.callback_query(F.data == "ai_toggle_morning")
async def ai_toggle_morning_cb(cb: CallbackQuery):
    st = await users_db.get_user_state(cb.from_user.id)
    new_val = not st.get("ai_morning_enabled", True)
    await users_db.save_user_state(cb.from_user.id, {"ai_morning_enabled": new_val})
    await cb.message.edit_reply_markup(reply_markup=ikb_ai_settings(new_val))
    await cb.answer("Оновлено!")


@router.callback_query(F.data == "ai_set_morning_time")
async def ai_set_morning_time_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AiSettings.setting_time)
    await cb.message.answer("⏰ Введи час ранкового плану як *гг:хх* (наприклад 08:30):", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AiSettings.setting_time)
async def ai_set_morning_time_save(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        return await msg.answer("⚠️ Невірний формат. Введи як *гг:хх*:", reply_markup=kb_cancel())
    await users_db.save_user_state(msg.from_user.id, {"morning_plan_time": raw})
    await state.clear()
    await msg.answer(
        f"✅ Особистий час ранкового плану встановлено: *{raw}*.\n\n"
        f"_Примітка: глобальний час у Render контролюється змінною AI_DAILY_PLAN_TIME; "
        f"ця настройка враховується плановиком, якщо він її підтримує._",
        reply_markup=kb_main(),
    )


@router.callback_query(F.data == "ai_usage_info")
async def ai_usage_info_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    usage = await ai_usage_db.get_usage(uid)
    used = usage.get("count", 0)
    await cb.message.edit_text(
        f"🤖 *AI запити сьогодні*\n\nВикористано: *{used} / {AI_DAILY_LIMIT}*\n"
        f"Залишилось: *{max(0, AI_DAILY_LIMIT - used)}*",
        reply_markup=ikb_ai_usage(used, AI_DAILY_LIMIT),
    )
    await cb.answer()


async def _safe_edit(cb: CallbackQuery, text: str):
    try:
        await cb.message.edit_text(text)
    except TelegramAPIError:
        pass