import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import LABELS, CATEGORIES, STATUS_PENDING, DB_ERROR_TEXT, AI_LIMIT_TEXT
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import projects as projects_db
from database import ai_usage as ai_usage_db
from handlers.common import require_auth, voice_task_drafts, parse_due
from keyboards.main_menu import kb_cancel
from keyboards.tasks import (
    kb_tasks_menu, kb_label, label_from_text,
    kb_category, category_from_text, kb_project_select,
)
from services import voice_task_parser
from services.planner_service import check_ai_limit

logger = logging.getLogger("tasks_bot")
router = Router(name="voice_task")

CANCEL_TEXT = "❌ Скасувати"


class VoiceTaskFlow(StatesGroup):
    waiting_voice = State()


class VoiceTaskEdit(StatesGroup):
    typing = State()


class VoiceTaskManualText(StatesGroup):
    waiting = State()


# =========================================================
# ФОРМАТУВАННЯ
# =========================================================

def _fmt_draft(d: dict) -> str:
    lines = [f"📝 {d['text']}"]
    if d.get("due"):
        due_dt = parse_due(d["due"])
        if due_dt:
            lines.append(f"📅 {due_dt.strftime('%d.%m.%Y')}")
            est = " (орієнтовно)" if d.get("time_is_estimated") else ""
            lines.append(f"⏰ {due_dt.strftime('%H:%M')}{est}")
    elif d.get("date_had_no_time"):
        lines.append("📅 Дата є, але час не визначено — постав через ⏰ Час")
    else:
        lines.append("📅 Без терміну")

    lbl = LABELS.get(d.get("label", "medium"), LABELS["medium"])
    lines.append(f"{lbl['emoji']} {lbl['name']}")
    cat = CATEGORIES.get(d.get("category", "other"), CATEGORIES["other"])
    lines.append(f"📂 {cat['name']}")

    if d.get("estimated_minutes"):
        h, m = divmod(d["estimated_minutes"], 60)
        dur = (f"{h} год " if h else "") + (f"{m} хв" if m else "")
        lines.append(f"⏱ {dur.strip()}")

    if d.get("project_title"):
        lines.append(f"📁 {d['project_title']}")

    if d.get("description"):
        lines.append(f"💬 {d['description']}")

    return "\n".join(lines)


def _fmt_multi(drafts: list) -> str:
    lines = []
    for i, d in enumerate(drafts, start=1):
        due_part = ""
        if d.get("due"):
            due_dt = parse_due(d["due"])
            if due_dt:
                due_part = f" — {due_dt.strftime('%d.%m %H:%M')}"
        lines.append(f"{i}. {d['text']}{due_part}")
    return "\n".join(lines)


# =========================================================
# КЛАВІАТУРИ
# =========================================================

def _ikb_quick_add() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Додати", callback_data="vtq_add"),
        InlineKeyboardButton(text="❌ Ні", callback_data="vtq_no"),
    ]])


def _ikb_single_confirm(idx: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Створити", callback_data=f"vtc:{idx}")],
        [
            InlineKeyboardButton(text="📝 Назву", callback_data=f"vtfield:{idx}:text"),
            InlineKeyboardButton(text="📅 Дату", callback_data=f"vtfield:{idx}:date"),
        ],
        [
            InlineKeyboardButton(text="⏰ Час", callback_data=f"vtfield:{idx}:time"),
            InlineKeyboardButton(text="🔴 Пріоритет", callback_data=f"vtfield:{idx}:label"),
        ],
        [
            InlineKeyboardButton(text="📂 Категорію", callback_data=f"vtfield:{idx}:category"),
            InlineKeyboardButton(text="📁 Проєкт", callback_data=f"vtfield:{idx}:project"),
        ],
        [InlineKeyboardButton(text="⏱ Тривалість", callback_data=f"vtfield:{idx}:duration")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="vt_cancel")],
    ])


def _ikb_multi() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Створити всі", callback_data="vt_create_all")],
        [InlineKeyboardButton(text="✏️ Перевірити", callback_data="vt_review")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="vt_cancel")],
    ])


def _ikb_review_list(drafts: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{i+1}. {d['text'][:30]}", callback_data=f"vted:{i}")]
            for i, d in enumerate(drafts)]
    rows.append([InlineKeyboardButton(text="✅ Створити всі", callback_data="vt_create_all")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="vt_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ikb_understanding_failed() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎙 Записати ще раз", callback_data="vt_retry")],
        [InlineKeyboardButton(text="⌨️ Ввести текстом", callback_data="vt_type_text")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="vt_cancel_flow")],
    ])


# =========================================================
# СТВОРЕННЯ ЗАДАЧІ В БД (та сама модель, що й ручне створення)
# =========================================================

async def _create_draft(uid: int, draft: dict, voice_meta: dict | None) -> dict | None:
    try:
        new_id = await tasks_db.next_task_id()
    except DBUnavailable:
        return None

    task = {
        "id": new_id,
        "uid": uid,
        "text": draft["text"],
        "label": draft["label"],
        "category": draft["category"],
        "due": draft.get("due", ""),
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
        "source": "voice",
        "project_id": draft.get("project_id"),
        "estimated_minutes": draft.get("estimated_minutes"),
    }
    if draft.get("description"):
        task["voice_description"] = draft["description"]
    if voice_meta:
        task["voice_meta"] = voice_meta

    try:
        await tasks_db.add_task(task)
        return await tasks_db.get_task(new_id)
    except DBUnavailable:
        return None


# =========================================================
# ЦЕНТРАЛЬНА ЛОГІКА (викликається і з кнопки, і пасивно з voice.py)
# =========================================================

async def _show_understanding_failed(msg: Message, heard_text: str):
    text = f"🤔 Не зовсім зрозумів задачу.\n\nЯ почув:\n«{heard_text}»"
    await msg.answer(text, reply_markup=_ikb_understanding_failed())


async def handle_voice_as_task(msg: Message, transcribed_text: str, explicit: bool) -> bool:
    """Повертає True, якщо повідомлення оброблено як (спроба) задачі —
    тоді voice.py НЕ повинен віддавати текст у звичайний AI-чат."""
    uid = msg.from_user.id

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        if explicit:
            await msg.answer(AI_LIMIT_TEXT)
            return True
        return False  # у пасивному режимі мовчки віддаємо в звичайний потік

    projects = await projects_db.get_active_projects(uid)
    result = await voice_task_parser.parse_voice_tasks(transcribed_text, projects)
    await ai_usage_db.increment_usage(uid)

    if not result["ok"] or not result["is_task"] or not result["drafts"]:
        if explicit:
            await _show_understanding_failed(msg, transcribed_text)
            return True
        return False

    drafts = result["drafts"]
    voice_meta = {"file_id": msg.voice.file_id, "duration": msg.voice.duration} if msg.voice else None

    if len(drafts) > 1:
        voice_task_drafts[uid] = {"drafts": drafts, "voice_meta": voice_meta}
        await msg.answer(
            f"🎙 Знайшов {len(drafts)} задачі. Ось що я зрозумів:\n\n{_fmt_multi(drafts)}\n\nСтворити всі?",
            reply_markup=_ikb_multi(),
        )
        return True

    draft = drafts[0]
    voice_task_drafts[uid] = {"drafts": [draft], "voice_meta": voice_meta}

    if not explicit and not draft["ambiguous"]:
        await msg.answer(
            f"🎙 Схоже, ти хочеш додати задачу:\n\n{_fmt_draft(draft)}\n\nДодати її?",
            reply_markup=_ikb_quick_add(),
        )
        return True

    if not draft["ambiguous"]:
        saved = await _create_draft(uid, draft, voice_meta)
        voice_task_drafts.pop(uid, None)
        if not saved:
            await msg.answer(DB_ERROR_TEXT, reply_markup=kb_tasks_menu())
        else:
            await msg.answer(f"✅ Задачу створено:\n\n{_fmt_draft(draft)}", reply_markup=kb_tasks_menu())
        return True

    await msg.answer(
        f"🎙 Я зрозумів так:\n\n{_fmt_draft(draft)}\n\nВсе правильно?",
        reply_markup=_ikb_single_confirm(0),
    )
    return True


# =========================================================
# ВХІД ЧЕРЕЗ КНОПКУ
# =========================================================

@router.message(F.text == "🎙 Голосова задача")
async def voice_task_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(VoiceTaskFlow.waiting_voice)
    await msg.answer(
        "🎙 Запиши голосове повідомлення з описом задачі "
        "(можна кілька одразу, наприклад «завтра о 15:00 зробити пост, і ввечері купити подарунок»).",
        reply_markup=kb_cancel(),
    )


@router.message(VoiceTaskFlow.waiting_voice, F.text == CANCEL_TEXT)
async def voice_task_cancel_wait(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_tasks_menu())


@router.message(VoiceTaskFlow.waiting_voice, F.voice)
async def voice_task_receive(msg: Message, state: FSMContext):
    from services import ai_service
    from config.constants import AI_ERROR_TEXT

    await state.clear()

    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_tasks_menu())
    if not ai_service.voice_available():
        return await msg.answer(
            "⚠️ Розпізнавання голосу не налаштоване. Потрібен WHISPER_API_KEY у змінних середовища.",
            reply_markup=kb_tasks_menu(),
        )

    wait_msg = await msg.answer("🎙 Розпізнаю голосове...")
    bot = msg.bot
    try:
        file = await bot.get_file(msg.voice.file_id)
        buf = await bot.download_file(file.file_path)
        audio_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити голосове (voice_task) для uid=%s", msg.from_user.id)
        return await wait_msg.edit_text("⚠️ Не вдалося завантажити голосове. Спробуй ще раз.")

    text = await ai_service.transcribe_voice(audio_bytes)
    if not text:
        return await wait_msg.edit_text(
            "🤔 Не вдалося розпізнати мову.",
            reply_markup=_ikb_understanding_failed(),
        )

    await wait_msg.delete()
    await handle_voice_as_task(msg, text, explicit=True)


@router.message(VoiceTaskFlow.waiting_voice)
async def voice_task_wrong_input(msg: Message):
    await msg.answer("🎙 Надішли саме голосове повідомлення (або натисни ❌ Скасувати).")


# =========================================================
# ПАСИВНИЙ РЕЖИМ — короткий сценарій
# =========================================================

@router.callback_query(F.data == "vtq_add")
async def vtq_add(cb: CallbackQuery):
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data:
        return await cb.answer("Задача застаріла, спробуй ще раз голосом.", show_alert=True)
    draft = state_data["drafts"][0]
    saved = await _create_draft(uid, draft, state_data.get("voice_meta"))
    voice_task_drafts.pop(uid, None)
    await cb.answer("Додано!")
    if not saved:
        return await cb.message.edit_text(DB_ERROR_TEXT)
    await cb.message.edit_text(f"✅ Задачу створено:\n\n{_fmt_draft(draft)}")


@router.callback_query(F.data == "vtq_no")
async def vtq_no(cb: CallbackQuery):
    voice_task_drafts.pop(cb.from_user.id, None)
    await cb.answer()
    await cb.message.edit_text("Гаразд, не додаю.")


# =========================================================
# ОДНА ЗАДАЧА — підтвердження / редагування
# =========================================================

@router.callback_query(F.data.startswith("vtc:"))
async def vt_create_one(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data or idx >= len(state_data["drafts"]):
        return await cb.answer("Не знайдено, спробуй ще раз голосом.", show_alert=True)

    draft = state_data["drafts"][idx]
    saved = await _create_draft(uid, draft, state_data.get("voice_meta"))
    if not saved:
        return await cb.message.edit_text(DB_ERROR_TEXT)

    await cb.answer("Створено!")
    await cb.message.edit_text(f"✅ Задачу створено:\n\n{_fmt_draft(draft)}")

    state_data["drafts"].pop(idx)
    if not state_data["drafts"]:
        voice_task_drafts.pop(uid, None)


@router.callback_query(F.data == "vt_cancel")
async def vt_cancel(cb: CallbackQuery):
    voice_task_drafts.pop(cb.from_user.id, None)
    await cb.answer("Скасовано")
    await cb.message.edit_text("❌ Скасовано.")


@router.callback_query(F.data == "vt_cancel_flow")
async def vt_cancel_flow(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    voice_task_drafts.pop(cb.from_user.id, None)
    await cb.answer("Скасовано")
    await cb.message.edit_text("❌ Скасовано.")


@router.callback_query(F.data == "vt_retry")
async def vt_retry(cb: CallbackQuery, state: FSMContext):
    voice_task_drafts.pop(cb.from_user.id, None)
    await cb.answer()
    await state.set_state(VoiceTaskFlow.waiting_voice)
    await cb.message.edit_text("🎙 Запиши голосове повідомлення ще раз.")


@router.callback_query(F.data == "vt_type_text")
async def vt_type_text(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(VoiceTaskManualText.waiting)
    await cb.message.edit_text("⌨️ Напиши текстом, що за задачу потрібно створити:")


@router.message(VoiceTaskManualText.waiting, F.text == CANCEL_TEXT)
async def vt_manual_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_tasks_menu())


@router.message(VoiceTaskManualText.waiting)
async def vt_manual_text_received(msg: Message, state: FSMContext):
    await state.clear()
    if not msg.text:
        return await msg.answer("⚠️ Напиши текстом опис задачі:")
    handled = await handle_voice_as_task(msg, msg.text, explicit=True)
    if not handled:
        await msg.answer("🤔 Не вдалося розпізнати задачу навіть із тексту.", reply_markup=kb_tasks_menu())


# =========================================================
# КІЛЬКА ЗАДАЧ
# =========================================================

@router.callback_query(F.data == "vt_create_all")
async def vt_create_all(cb: CallbackQuery):
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data:
        return await cb.answer("Задачі застаріли, спробуй ще раз.", show_alert=True)

    created = 0
    for draft in state_data["drafts"]:
        saved = await _create_draft(uid, draft, state_data.get("voice_meta"))
        if saved:
            created += 1

    voice_task_drafts.pop(uid, None)
    await cb.answer()
    await cb.message.edit_text(f"✅ Створено задач: {created}/{len(state_data['drafts'])}")


@router.callback_query(F.data == "vt_review")
async def vt_review(cb: CallbackQuery):
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data:
        return await cb.answer("Задачі застаріли, спробуй ще раз.", show_alert=True)
    await cb.answer()
    await cb.message.edit_text("Обери задачу для перевірки:", reply_markup=_ikb_review_list(state_data["drafts"]))


@router.callback_query(F.data.startswith("vted:"))
async def vt_edit_open(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data or idx >= len(state_data["drafts"]):
        return await cb.answer("Не знайдено", show_alert=True)
    draft = state_data["drafts"][idx]
    await cb.answer()
    await cb.message.edit_text(_fmt_draft(draft), reply_markup=_ikb_single_confirm(idx))


# =========================================================
# РЕДАГУВАННЯ ОКРЕМОГО ПОЛЯ
# =========================================================

@router.callback_query(F.data.startswith("vtfield:"))
async def vt_edit_field(cb: CallbackQuery, state: FSMContext):
    _, idx_s, field = cb.data.split(":", 2)
    idx = int(idx_s)
    uid = cb.from_user.id
    state_data = voice_task_drafts.get(uid)
    if not state_data or idx >= len(state_data["drafts"]):
        return await cb.answer("Не знайдено", show_alert=True)

    await state.set_state(VoiceTaskEdit.typing)
    await state.update_data(vt_idx=idx, vt_field=field)
    await cb.answer()

    labels = {
        "text": "Назву", "date": "Дату (дд.мм.рррр)", "time": "Час (гг:хх)",
        "label": "Пріоритет", "category": "Категорію", "project": "Проєкт",
        "duration": "Тривалість у хвилинах",
    }
    if field == "label":
        kb = kb_label()
    elif field == "category":
        kb = kb_category()
    elif field == "project":
        projects = await projects_db.get_active_projects(uid)
        await state.update_data(vt_projects=[{"id": str(p["_id"]), "title": p.get("title", "")} for p in projects])
        kb = kb_project_select(projects)
    else:
        kb = kb_cancel()
    await cb.message.answer(f"Введи нове значення для «{labels.get(field, field)}»:", reply_markup=kb)


@router.message(VoiceTaskEdit.typing)
async def vt_edit_save(msg: Message, state: FSMContext):
    fd = await state.get_data()
    idx, field = fd["vt_idx"], fd["vt_field"]
    uid = msg.from_user.id
    state_data = voice_task_drafts.get(uid)

    if msg.text == CANCEL_TEXT or not state_data or idx >= len(state_data["drafts"]):
        await state.clear()
        if state_data and idx < len(state_data["drafts"]):
            return await msg.answer(_fmt_draft(state_data["drafts"][idx]), reply_markup=_ikb_single_confirm(idx))
        return await msg.answer("Скасовано.", reply_markup=kb_tasks_menu())

    draft = state_data["drafts"][idx]

    if field == "text":
        if not msg.text:
            return await msg.answer("⚠️ Введи текстову назву:", reply_markup=kb_cancel())
        draft["text"] = msg.text.strip()[:200]

    elif field == "date":
        try:
            d = datetime.strptime(msg.text.strip(), "%d.%m.%Y").date()
        except (ValueError, TypeError):
            return await msg.answer("⚠️ Формат дд.мм.рррр. Спробуй ще раз:", reply_markup=kb_cancel())
        due_dt = parse_due(draft.get("due", ""))
        old_time = due_dt.strftime("%H:%M") if due_dt else "12:00"
        draft["due"] = f"{d.strftime('%d.%m.%Y')} {old_time}"
        draft["date_had_no_time"] = False
        draft["time_is_estimated"] = False

    elif field == "time":
        try:
            datetime.strptime(msg.text.strip(), "%H:%M")
        except (ValueError, TypeError):
            return await msg.answer("⚠️ Формат гг:хх. Спробуй ще раз:", reply_markup=kb_cancel())
        due_dt = parse_due(draft.get("due", ""))
        date_part = due_dt.strftime("%d.%m.%Y") if due_dt else datetime.now().strftime("%d.%m.%Y")
        draft["due"] = f"{date_part} {msg.text.strip()}"
        draft["date_had_no_time"] = False
        draft["time_is_estimated"] = False

    elif field == "label":
        label = label_from_text(msg.text)
        if not label:
            return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_label())
        draft["label"] = label

    elif field == "category":
        category = category_from_text(msg.text)
        if not category:
            return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_category())
        draft["category"] = category

    elif field == "project":
        projects_cache = fd.get("vt_projects", [])
        if msg.text == "📋 Без проекту":
            draft["project_id"] = None
            draft["project_title"] = None
        else:
            title = msg.text[2:].strip() if msg.text and msg.text.startswith("📁 ") else (msg.text or "").strip()
            match = next((p for p in projects_cache if p["title"][:64] == title), None)
            if not match:
                return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_cancel())
            draft["project_id"] = match["id"]
            draft["project_title"] = match["title"]

    elif field == "duration":
        try:
            minutes = int(msg.text.strip())
        except (ValueError, TypeError):
            return await msg.answer("⚠️ Введи число хвилин:", reply_markup=kb_cancel())
        draft["estimated_minutes"] = max(5, min(minutes, 480))

    draft["ambiguous"] = False
    await state.clear()
    await msg.answer(f"Оновлено:\n\n{_fmt_draft(draft)}", reply_markup=_ikb_single_confirm(idx))