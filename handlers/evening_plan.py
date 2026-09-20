

import asyncio
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import LABELS, AI_ERROR_TEXT, AI_LIMIT_TEXT
from database.mongo import DBUnavailable
from services import planner_service
from keyboards.evening_plan import ikb_evening_hours, ikb_evening_generating, ikb_evening_plan_preview
from keyboards.main_menu import kb_cancel, kb_main
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="evening_plan")

EVENING_PLAN_TIMEOUT_SECONDS = 100

evening_plan_cache: dict[int, dict] = {}
_generation_tasks: dict[int, asyncio.Task] = {}


class EveningHoursInput(StatesGroup):
    answer = State()


class EveningAddTask(StatesGroup):
    text = State()


def _fmt_evening_plan_preview(plan: dict, selected: set) -> str:
    tasks = plan.get("tasks", [])
    total_minutes = sum(t.get("estimated_minutes", 30) for i, t in enumerate(tasks) if i in selected)
    lines = [f"📅 *План на завтра — {plan.get('hours')} год*", ""]
    if plan.get("focus"):
        lines.append(f"🎯 Фокус: *{plan['focus']}*")
        lines.append("")
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t.get("label", ""), {})
        lines.append(f"{mark} {i + 1}. {label.get('emoji','')} {t['text']} (~{t.get('estimated_minutes', 30)} хв)")
    h, m = divmod(total_minutes, 60)
    load_parts = ([f"{h} год"] if h else []) + ([f"{m} хв"] if m else [])
    lines.append("")
    lines.append(f"📊 Разом: ~{' '.join(load_parts) or '0 хв'}")
    lines.append("")
    lines.append("Тисни на задачу, щоб зняти/додати позначку, потім підтверди.")
    return "\n".join(lines)


async def _generate_and_show(target, uid: int, hours: float, edit: bool):
    allowed, _ = await planner_service.check_ai_limit(uid)
    if not allowed:
        if edit:
            return await target.message.edit_text(AI_LIMIT_TEXT)
        return await target.answer(AI_LIMIT_TEXT)

    generating_text = "🌙 Аналізую активні задачі, проєкти та цілі, формую план на завтра..."
    if edit:
        status_msg = target.message
        try:
            await status_msg.edit_text(generating_text, reply_markup=ikb_evening_generating())
        except TelegramAPIError:
            status_msg = await target.message.answer(generating_text, reply_markup=ikb_evening_generating())
    else:
        status_msg = await target.answer(generating_text, reply_markup=ikb_evening_generating())

    task = asyncio.create_task(planner_service.generate_tomorrow_plan(uid, hours))
    _generation_tasks[uid] = task
    try:
        plan = await asyncio.wait_for(task, timeout=EVENING_PLAN_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return
    except asyncio.TimeoutError:
        task.cancel()
        return await status_msg.edit_text("⚠️ AI не відповів вчасно. Спробуй ще раз трохи пізніше.")
    except Exception:
        logger.exception("Помилка генерації вечірнього плану для uid=%s", uid)
        return await status_msg.edit_text(AI_ERROR_TEXT)
    finally:
        _generation_tasks.pop(uid, None)

    if not plan or not plan.get("tasks"):
        return await status_msg.edit_text(
            "⚠️ Не вдалось сформувати новий план — можливо, усі логічні задачі на завтра вже є "
            "серед активних. Спробуй «🔄 Перегенерувати» або додай задачу вручну кнопкою «➕»."
        )

    selected = set(range(len(plan["tasks"])))
    evening_plan_cache[uid] = {"plan": plan, "selected": selected}
    await status_msg.edit_text(
        _fmt_evening_plan_preview(plan, selected),
        reply_markup=ikb_evening_plan_preview(plan["tasks"], selected),
    )


@router.callback_query(F.data.startswith("evplan_hours:"))
async def evplan_hours_cb(cb: CallbackQuery, state: FSMContext):
    if not await require_auth(cb.message, state):
        return await cb.answer()
    hours = int(cb.data.split(":")[1])
    await cb.answer()
    await _generate_and_show(cb, cb.from_user.id, hours, edit=True)


@router.callback_query(F.data == "evplan_hours_custom")
async def evplan_hours_custom_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(EveningHoursInput.answer)
    await cb.message.answer("✏️ Напиши, скільки годин плануєш працювати завтра (число):", reply_markup=kb_cancel())


@router.message(EveningHoursInput.answer)
async def evplan_hours_custom_input(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = (msg.text or "").strip().replace(",", ".")
    try:
        hours = float(raw)
    except ValueError:
        return await msg.answer("⚠️ Напиши число, наприклад: 5", reply_markup=kb_cancel())
    if not (0 < hours <= 16):
        return await msg.answer("⚠️ Введи реалістичну кількість годин (від 0 до 16):", reply_markup=kb_cancel())
    await state.clear()
    await msg.answer("Приймаю в роботу...", reply_markup=kb_main())
    await _generate_and_show(msg, msg.from_user.id, hours, edit=False)


@router.callback_query(F.data == "evplan_gen_cancel")
async def evplan_gen_cancel_cb(cb: CallbackQuery):
    await cb.answer("Скасовую...")
    uid = cb.from_user.id
    task = _generation_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()
    try:
        await cb.message.edit_text("❌ Генерацію скасовано.")
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("evptoggle:"))
async def evptoggle_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = evening_plan_cache.get(uid)
    if not data:
        return await cb.answer("Сесія застаріла, згенеруй план заново.", show_alert=True)
    idx = int(cb.data.split(":")[1])
    sel = data["selected"]
    if idx in sel:
        sel.discard(idx)
    else:
        sel.add(idx)
    plan = data["plan"]
    await cb.message.edit_text(
        _fmt_evening_plan_preview(plan, sel),
        reply_markup=ikb_evening_plan_preview(plan["tasks"], sel),
    )
    await cb.answer()


@router.callback_query(F.data == "evp_regenerate")
async def evp_regenerate_cb(cb: CallbackQuery):
    data = evening_plan_cache.get(cb.from_user.id)
    if not data:
        return await cb.answer("Сесія застаріла.", show_alert=True)
    hours = data["plan"].get("hours", 4)
    await cb.answer("Перегенеровую...")
    await _generate_and_show(cb, cb.from_user.id, hours, edit=True)


@router.callback_query(F.data == "evp_change_hours")
async def evp_change_hours_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("🌙 Скільки годин плануєш працювати завтра? Обери варіант:", reply_markup=ikb_evening_hours())


@router.callback_query(F.data == "evp_add_task")
async def evp_add_task_cb(cb: CallbackQuery, state: FSMContext):
    data = evening_plan_cache.get(cb.from_user.id)
    if not data:
        return await cb.answer("Сесія застаріла.", show_alert=True)
    await state.set_state(EveningAddTask.text)
    await cb.answer()
    await cb.message.answer("✏️ Напиши текст нового завдання на завтра:", reply_markup=kb_cancel())


@router.message(EveningAddTask.text)
async def evp_add_task_save(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if msg.text == "❌ Скасувати":
        await state.clear()
        data = evening_plan_cache.get(uid)
        if not data:
            return await msg.answer("Скасовано.", reply_markup=kb_main())
        return await msg.answer(
            _fmt_evening_plan_preview(data["plan"], data["selected"]),
            reply_markup=ikb_evening_plan_preview(data["plan"]["tasks"], data["selected"]),
        )

    text = (msg.text or "").strip()[:200]
    await state.clear()
    if not text:
        return await msg.answer("⚠️ Текст не може бути порожнім.", reply_markup=kb_main())

    data = evening_plan_cache.get(uid)
    if not data:
        return await msg.answer("Сесія застаріла, згенеруй план заново.", reply_markup=kb_main())

    tasks = data["plan"]["tasks"]
    tasks.append({"text": text, "label": "medium", "category": "other", "estimated_minutes": 30})
    data["selected"].add(len(tasks) - 1)
    await msg.answer(
        _fmt_evening_plan_preview(data["plan"], data["selected"]),
        reply_markup=ikb_evening_plan_preview(tasks, data["selected"]),
    )


@router.callback_query(F.data == "evp_confirm")
async def evp_confirm_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = evening_plan_cache.pop(uid, None)
    if not data or not data["selected"]:
        return await cb.answer("Нічого не обрано.", show_alert=True)
    selected_tasks = [data["plan"]["tasks"][i] for i in sorted(data["selected"])]
    try:
        added = await planner_service.save_tomorrow_tasks(uid, selected_tasks)
    except DBUnavailable:
        return await cb.message.edit_text(AI_ERROR_TEXT)
    skipped = len(selected_tasks) - added
    text = f"✅ *План на завтра збережено!*\n\nДодано {added} задач(і)."
    if skipped:
        text += f"\n🔁 Пропущено {skipped} — вже є схожа задача на завтра."
    await cb.message.edit_text(text)
    await cb.answer()


@router.callback_query(F.data == "evp_cancel")
async def evp_cancel_cb(cb: CallbackQuery):
    evening_plan_cache.pop(cb.from_user.id, None)
    await cb.message.edit_text("❌ План відхилено. Задачі не додано.")
    await cb.answer()