import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import PROJECT_ACTIVE, DB_ERROR_TEXT, DEFAULT_CURRENCY
from database.mongo import DBUnavailable
from database import projects as projects_db
from database import tasks as tasks_db
from services import project_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.projects import (
    ikb_projects,
    ikb_project_actions,
    ikb_stages_list,
    ikb_stage_actions,
    ikb_stages_ai_confirm,
)
from handlers.common import require_auth, fmt_task, user_list_cache

logger = logging.getLogger("tasks_bot")
router = Router(name="projects")


class AddProject(StatesGroup):
    title = State()
    description = State()
    has_budget = State()
    budget = State()


class AddStage(StatesGroup):
    title = State()
    description = State()


class EditStage(StatesGroup):
    title = State()
    description = State()


def _fmt_projects_list(projects: list) -> str:
    if not projects:
        return (
            "📁 *Мої проєкти*\n\n"
            "Ще немає жодного проєкту.\n"
            "Натисни «➕ Додати проєкт», щоб створити перший — AI буде бачити прогрес і давати поради."
        )
    lines = ["📁 *Мої проєкти*", ""]
    for p in projects:
        status = "🟢 Активний" if p.get("status") == PROJECT_ACTIVE else "✅ Завершено"
        lines.append(f"*{p.get('title','')}* — {status}")
        if p.get("description"):
            lines.append(f"   _{p['description'][:100]}_")
        if p.get("budget"):
            lines.append(f"   💰 {p.get('spent',0)} / {p.get('budget')} {DEFAULT_CURRENCY}")
        stages = p.get("stages") or []
        if stages:
            done, total = projects_db.stage_progress(p)
            current = projects_db.get_current_stage(p)
            if current:
                lines.append(f"   🔵 Поточний етап: {current.get('title','')}")
            lines.append(f"   Прогрес: {done}/{total} етапи")
        lines.append("")
    return "\n".join(lines).strip()


async def _fmt_project_detail(uid: int, p: dict) -> str:
    status = "🟢 Активний" if p.get("status") == PROJECT_ACTIVE else "✅ Завершено"
    lines = [f"📁 *{p.get('title','')}*", "", f"Статус: {status}"]
    if p.get("description"):
        lines.append(f"\n📝 {p['description']}")
    progress = await project_service.get_project_progress(uid, str(p["_id"]))
    lines.append("")
    lines.append(f"📋 Задач: {progress['done']} / {progress['total']} ({progress['percent']}%)")

    stages = p.get("stages") or []
    if stages:
        done, total = projects_db.stage_progress(p)
        current = projects_db.get_current_stage(p)
        lines.append("")
        if current:
            lines.append(f"🔵 Поточний етап: *{current.get('title','')}*")
            if current.get("description"):
                lines.append(current["description"])
        else:
            lines.append("🎉 Усі етапи завершено!")
        lines.append(f"Прогрес: {done}/{total} етапи")

    if p.get("budget"):
        percent = project_service.budget_percent(p)
        bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
        lines.append("")
        lines.append(f"💰 Бюджет: {p.get('spent',0)} / {p.get('budget')} {DEFAULT_CURRENCY}")
        lines.append(f"{bar} {percent}%")
    if p.get("deadline"):
        lines.append(f"\n📅 Дедлайн: {p['deadline']}")
    return "\n".join(lines)


@router.message(F.text == "📁 Мої проєкти")
async def projects_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        projects = await projects_db.get_all_projects(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer(_fmt_projects_list(projects), reply_markup=ikb_projects(projects))


@router.callback_query(F.data == "proj_close")
async def proj_close_cb(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await cb.answer()


@router.callback_query(F.data == "projects_menu")
async def projects_menu_cb(cb: CallbackQuery):
    try:
        projects = await projects_db.get_all_projects(cb.from_user.id)
        await cb.message.edit_text(_fmt_projects_list(projects), reply_markup=ikb_projects(projects))
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


@router.callback_query(F.data.startswith("projopen:"))
async def project_open_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        active = p.get("status") == PROJECT_ACTIVE
        text = await _fmt_project_detail(cb.from_user.id, p)
        await cb.message.edit_text(text, reply_markup=ikb_project_actions(pid, active))
        await cb.answer()
    except Exception:
        logger.exception("project_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("projtoggle:"))
async def project_toggle_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        new_active = p.get("status") != PROJECT_ACTIVE
        await project_service.toggle_project_status(pid, new_active)
        projects = await projects_db.get_all_projects(cb.from_user.id)
        await cb.message.edit_text(_fmt_projects_list(projects), reply_markup=ikb_projects(projects))
        await cb.answer("Оновлено")
    except Exception:
        logger.exception("project_toggle_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("projdel:"))
async def project_delete_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        await project_service.remove_project(pid)
        projects = await projects_db.get_all_projects(cb.from_user.id)
        await cb.message.edit_text(_fmt_projects_list(projects), reply_markup=ikb_projects(projects))
        await cb.answer("Видалено")
    except Exception:
        logger.exception("project_delete_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("projtasks:"))
async def project_tasks_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        uid = cb.from_user.id
        tasks = await tasks_db.get_project_tasks(uid, pid)
        if not tasks:
            await cb.answer("У цього проєкту ще немає задач.", show_alert=True)
            return
        text_lines = ["📋 *Задачі проєкту*", ""]
        for t in tasks[:30]:
            icon = "✅" if t.get("status") == "done" else "⏳"
            text_lines.append(f"{icon} №{t.get('id')} — {t.get('text','')[:40]}")
        await cb.message.answer("\n".join(text_lines))
        await cb.answer()
    except Exception:
        logger.exception("project_tasks_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("projbudget:"))
async def project_budget_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        if not p.get("budget"):
            await cb.answer("У цього проєкту не задано бюджет.", show_alert=True)
            return
        percent = project_service.budget_percent(p)
        bar = "█" * round(percent / 10) + "░" * (10 - round(percent / 10))
        await cb.message.answer(
            f"💰 *Бюджет проєкту «{p.get('title','')}»*\n\n"
            f"{bar} {percent}%\n"
            f"Витрачено: {p.get('spent',0)} / {p.get('budget')} {DEFAULT_CURRENCY}"
        )
        await cb.answer()
    except Exception:
        logger.exception("project_budget_cb failed")
        await _safe_alert(cb)


# =========================================================
# ЕТАПИ ПРОЄКТУ
# =========================================================

def _fmt_stage_detail(p: dict, stage: dict) -> str:
    stages = p.get("stages") or []
    idx = next((i for i, s in enumerate(stages) if s.get("id") == stage.get("id")), 0)
    current = projects_db.get_current_stage(p)
    if stage.get("status") == "done":
        status_text = "✅ Завершено"
    elif current and current.get("id") == stage.get("id"):
        status_text = "🔵 В процесі"
    else:
        status_text = "⏳ Очікує"
    lines = [
        f"🧩 *Етап {idx + 1}/{len(stages)}: {stage.get('title','')}*",
        "",
        f"Статус: {status_text}",
    ]
    if stage.get("description"):
        lines.append(f"\n📝 {stage['description']}")
    return "\n".join(lines)


async def _reopen_stage(cb: CallbackQuery, pid: str, stage_id: str, answer_text: str = "Оновлено"):
    p = await projects_db.get_project(pid)
    stages = (p or {}).get("stages") or []
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if not stage:
        return await cb.answer("Етап не знайдено.", show_alert=True)
    idx = next((i for i, s in enumerate(stages) if s.get("id") == stage_id), 0)
    done = stage.get("status") == "done"
    await cb.message.edit_text(
        _fmt_stage_detail(p, stage),
        reply_markup=ikb_stage_actions(pid, stage_id, done, can_up=idx > 0, can_down=idx < len(stages) - 1),
    )
    await cb.answer(answer_text)


@router.callback_query(F.data.startswith("projstages:"))
async def project_stages_cb(cb: CallbackQuery):
    try:
        pid = cb.data.split(":", 1)[1]
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        stages = p.get("stages") or []
        text = f"🧩 *Етапи проєкту «{p.get('title','')}»*\n\n"
        if not stages:
            text += (
                "Ще немає жодного етапу.\n"
                "Додай перший вручну або натисни «✨ Створити етапи через AI» — "
                "AI запропонує логічну послідовність етапів під цей проєкт."
            )
        else:
            done, total = projects_db.stage_progress(p)
            text += f"Прогрес: {done}/{total}\n\nТисни на етап, щоб побачити опис і керувати ним."
        await cb.message.edit_text(text, reply_markup=ikb_stages_list(pid, stages))
        await cb.answer()
    except Exception:
        logger.exception("project_stages_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stageopen:"))
async def stage_open_cb(cb: CallbackQuery):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        stages = p.get("stages") or []
        stage = next((s for s in stages if s.get("id") == stage_id), None)
        if not stage:
            return await cb.answer("Етап не знайдено.", show_alert=True)
        idx = next((i for i, s in enumerate(stages) if s.get("id") == stage_id), 0)
        done = stage.get("status") == "done"
        await cb.message.edit_text(
            _fmt_stage_detail(p, stage),
            reply_markup=ikb_stage_actions(pid, stage_id, done, can_up=idx > 0, can_down=idx < len(stages) - 1),
        )
        await cb.answer()
    except Exception:
        logger.exception("stage_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stagetoggle:"))
async def stage_toggle_cb(cb: CallbackQuery):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        stage = await projects_db.get_stage(pid, stage_id)
        if not stage:
            return await cb.answer("Етап не знайдено.", show_alert=True)
        new_status = "pending" if stage.get("status") == "done" else "done"
        await projects_db.update_stage(pid, stage_id, {"status": new_status})
        await _reopen_stage(cb, pid, stage_id)
    except Exception:
        logger.exception("stage_toggle_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stageup:"))
async def stage_up_cb(cb: CallbackQuery):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        moved = await projects_db.reorder_stage(pid, stage_id, "up")
        await _reopen_stage(cb, pid, stage_id, "Переміщено" if moved else "Це вже перший етап")
    except Exception:
        logger.exception("stage_up_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stagedown:"))
async def stage_down_cb(cb: CallbackQuery):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        moved = await projects_db.reorder_stage(pid, stage_id, "down")
        await _reopen_stage(cb, pid, stage_id, "Переміщено" if moved else "Це вже останній етап")
    except Exception:
        logger.exception("stage_down_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stagedel:"))
async def stage_delete_cb(cb: CallbackQuery):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        await projects_db.delete_stage(pid, stage_id)
        p = await projects_db.get_project(pid)
        stages = (p or {}).get("stages") or []
        await cb.message.edit_text(
            f"🧩 *Етапи проєкту «{(p or {}).get('title','')}»*\n\nЕтап видалено.",
            reply_markup=ikb_stages_list(pid, stages),
        )
        await cb.answer("Видалено")
    except Exception:
        logger.exception("stage_delete_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stage_add:"))
async def stage_add_start(cb: CallbackQuery, state: FSMContext):
    pid = cb.data.split(":", 1)[1]
    await state.set_state(AddStage.title)
    await state.update_data(pid=pid)
    await cb.message.answer("🧩 Введи назву етапу (коротко, напр. «Дизайн», «Верстка», «Тестування»):", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AddStage.title)
async def stage_add_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(title=msg.text.strip()[:100])
    await state.set_state(AddStage.description)
    await msg.answer(
        "📝 Опиши, що саме треба зробити на цьому етапі (це побачить AI при генерації задач). "
        "Або напиши «-», щоб пропустити:",
        reply_markup=kb_cancel(),
    )


@router.message(AddStage.description)
async def stage_add_description(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:400]
    fd = await state.get_data()
    pid = fd["pid"]
    title = fd["title"]
    await state.clear()

    try:
        await projects_db.add_stage(pid, title, desc)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    p = await projects_db.get_project(pid)
    stages = (p or {}).get("stages") or []
    await msg.answer("✅ Етап додано!", reply_markup=kb_main())
    await msg.answer(
        f"🧩 *Етапи проєкту «{(p or {}).get('title','')}»*\n\nПрогрес: 0/{len(stages)}",
        reply_markup=ikb_stages_list(pid, stages),
    )


# --- ✏️ Редагування етапу ---

@router.callback_query(F.data.startswith("stageedit:"))
async def stage_edit_start(cb: CallbackQuery, state: FSMContext):
    try:
        _, pid, stage_id = cb.data.split(":", 2)
        stage = await projects_db.get_stage(pid, stage_id)
        if not stage:
            return await cb.answer("Етап не знайдено.", show_alert=True)
        await state.set_state(EditStage.title)
        await state.update_data(pid=pid, stage_id=stage_id)
        await cb.message.answer(
            f"✏️ Поточна назва: «{stage.get('title','')}»\n\nВведи нову назву, або «-», щоб залишити без змін:",
            reply_markup=kb_cancel(),
        )
        await cb.answer()
    except Exception:
        logger.exception("stage_edit_start failed")
        await _safe_alert(cb)


@router.message(EditStage.title)
async def stage_edit_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    new_title = None if msg.text.strip() == "-" else msg.text.strip()[:100]
    await state.update_data(new_title=new_title)
    await state.set_state(EditStage.description)
    fd = await state.get_data()
    stage = await projects_db.get_stage(fd["pid"], fd["stage_id"])
    cur_desc = (stage or {}).get("description") or "(порожньо)"
    await msg.answer(
        f"📝 Поточний опис: «{cur_desc}»\n\nВведи новий опис, або «-», щоб залишити без змін:",
        reply_markup=kb_cancel(),
    )


@router.message(EditStage.description)
async def stage_edit_description(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    fd = await state.get_data()
    pid, stage_id = fd["pid"], fd["stage_id"]
    new_title = fd.get("new_title")
    new_desc = None if msg.text.strip() == "-" else msg.text.strip()[:400]
    await state.clear()

    try:
        await projects_db.edit_stage(pid, stage_id, title=new_title, description=new_desc)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    p = await projects_db.get_project(pid)
    stages = (p or {}).get("stages") or []
    await msg.answer("✅ Етап оновлено!", reply_markup=kb_main())
    await msg.answer(
        f"🧩 *Етапи проєкту «{(p or {}).get('title','')}»*",
        reply_markup=ikb_stages_list(pid, stages),
    )


# --- ✨ AI-генерація етапів ---

@router.callback_query(F.data.startswith("stages_ai:"))
async def stages_ai_cb(cb: CallbackQuery, state: FSMContext):
    try:
        pid = cb.data.split(":", 1)[1]
        p = await projects_db.get_project(pid)
        if not p:
            return await cb.answer("Не знайдено", show_alert=True)
        await cb.answer("Генерую етапи…")
        await cb.message.edit_text("✨ AI аналізує проєкт і формує етапи, зачекай кілька секунд…")

        stages = await project_service.generate_stages_ai(p.get("title", ""), p.get("description", ""))
        if not stages:
            return await cb.message.edit_text(
                "⚠️ Не вдалося згенерувати етапи через AI. Спробуй ще раз або додай етап вручну.",
                reply_markup=ikb_stages_list(pid, p.get("stages") or []),
            )

        await state.update_data(ai_stages=stages, ai_pid=pid)
        lines = ["✨ *AI пропонує такі етапи:*", ""]
        for i, s in enumerate(stages, 1):
            lines.append(f"{i}. *{s['title']}*")
            if s.get("description"):
                lines.append(f"   _{s['description']}_")
        lines.append("")
        lines.append("Підтверди, щоб зберегти їх у проєкт.")
        await cb.message.edit_text("\n".join(lines), reply_markup=ikb_stages_ai_confirm(pid))
    except Exception:
        logger.exception("stages_ai_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stages_ai_confirm:"))
async def stages_ai_confirm_cb(cb: CallbackQuery, state: FSMContext):
    try:
        pid = cb.data.split(":", 1)[1]
        fd = await state.get_data()
        stages = fd.get("ai_stages") or []
        if not stages or fd.get("ai_pid") != pid:
            return await cb.answer("Немає збережених пропозицій, згенеруй ще раз.", show_alert=True)

        await project_service.save_ai_stages(pid, stages)
        await state.update_data(ai_stages=None, ai_pid=None)

        p = await projects_db.get_project(pid)
        stage_list = (p or {}).get("stages") or []
        done, total = projects_db.stage_progress(p)
        await cb.message.edit_text(
            f"✅ Додано {len(stages)} етапів!\n\n"
            f"🧩 *Етапи проєкту «{(p or {}).get('title','')}»*\n\nПрогрес: {done}/{total}",
            reply_markup=ikb_stages_list(pid, stage_list),
        )
        await cb.answer("Збережено")
    except Exception:
        logger.exception("stages_ai_confirm_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("stages_ai_cancel:"))
async def stages_ai_cancel_cb(cb: CallbackQuery, state: FSMContext):
    try:
        pid = cb.data.split(":", 1)[1]
        await state.update_data(ai_stages=None, ai_pid=None)
        p = await projects_db.get_project(pid)
        stages = (p or {}).get("stages") or []
        await cb.message.edit_text(
            f"🧩 *Етапи проєкту «{(p or {}).get('title','')}»*\n\nСкасовано.",
            reply_markup=ikb_stages_list(pid, stages),
        )
        await cb.answer()
    except Exception:
        logger.exception("stages_ai_cancel_cb failed")
        await _safe_alert(cb)


# =========================================================
# ДОДАВАННЯ ПРОЄКТУ
# =========================================================

@router.callback_query(F.data == "proj_add")
async def project_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddProject.title)
    await cb.message.answer("📁 Введи назву проєкту (коротко):", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AddProject.title)
async def project_add_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(title=msg.text.strip()[:100])
    await state.set_state(AddProject.description)
    await msg.answer("📝 Опиши проєкт детальніше (або напиши «-», щоб пропустити):", reply_markup=kb_cancel())


@router.message(AddProject.description)
async def project_add_desc(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:300]
    await state.update_data(description=desc)
    await state.set_state(AddProject.has_budget)
    await msg.answer(
        f"💰 Задати бюджет проєкту в {DEFAULT_CURRENCY}? Введи число, або напиши «-», щоб пропустити:",
        reply_markup=kb_cancel(),
    )


@router.message(AddProject.has_budget)
async def project_add_budget(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    budget = None
    if msg.text.strip() != "-":
        raw = msg.text.strip().replace(",", ".").replace(" ", "")
        try:
            budget = float(raw)
            if budget <= 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ Введи додатнє число або «-»:", reply_markup=kb_cancel())
    fd = await state.get_data()
    try:
        await project_service.create_project(
            uid=msg.from_user.id,
            title=fd["title"],
            description=fd.get("description", ""),
            budget=budget,
        )
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer(
        "✅ Проєкт додано! AI буде враховувати його при плануванні та аналізі.\n\n"
        "💡 Порада: додай етапи проєкту (кнопка «🧩 Етапи проєкту» в картці проєкту) — "
        "тоді AI генеруватиме задачі саме під поточний етап, а не абстрактні задачі по проєкту.",
        reply_markup=kb_main(),
    )


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass