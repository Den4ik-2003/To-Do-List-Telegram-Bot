"""
НОВИЙ ФАЙЛ: handlers/ai_developer.py

Хендлери фічі "👨‍💻 AI Developer". Точка входу — callback "aidev_open:{pid}",
яку додано ОДНІЄЮ кнопкою до вже існуючої картки проєкту в
handlers/github_deploy.py (gh_proj_open) — нового екрану "оберіть проєкт"
тут немає, використовується вже готовий "📚 Мої проєкти" → картка проєкту.

Нічого не дублює:
- GitHub-токен: github_projects_db.get_credential() + github_crypto —
  ТОЙ САМИЙ, що й у деплої/завантаженні.
- Читання/запис у GitHub: services/ai_developer_service.py, який сам
  усередині використовує github_api.deploy_files / get_tree_recursive /
  get_blob_content — ті самі функції, що й Download ZIP / Deploy.
- AI-ліміт: database/ai_usage.py + services/planner_service.check_ai_limit —
  той самий денний ліміт AI-запитів, що й у ранковому плані.

Обмеження, які варто знати:
- Знімок репозиторію (файли для AI-контексту) кешується в пам'яті процесу
  на юзера/проєкт (_snapshot_cache), щоб не перечитувати GitHub на кожну
  кнопку в межах однієї сесії. "🔄 Оновити з GitHub" примусово скидає кеш.
- "↩️ Скасувати останню зміну" використовує "до"-знімок файлів, який
  живе ТІЛЬКИ в пам'яті процесу (_last_change) — переживає до рестарту
  бота, як і решта _pending_*-станів у github_deploy.py. Якщо бот
  перезапускався після AI-зміни — undo для неї вже недоступний.
- Файли, які AI СТВОРИВ (не було "до"-версії), undo не видаляє — про це
  бот прямо попереджає.

⚠️ Роутер `router` (name="ai_developer") потрібно зареєструвати в
диспетчері поруч з іншими (dp.include_router(...)) — я не бачив файл,
де це робиться (main.py / bot.py), тож сам це не дописав.
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config.settings import AI_DAILY_LIMIT
from database import ai_usage as ai_usage_db
from database import github_projects as github_projects_db
from services import ai_service, ai_developer_service, github_crypto
from services.planner_service import check_ai_limit
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.ai_developer import (
    ikb_ai_developer_menu, ikb_back_to_ai_menu, ikb_change_confirm, ikb_undo_confirm,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="ai_developer")

# _snapshot_cache: uid -> {"project_id": str, "snapshot": {...}}
_snapshot_cache: dict[int, dict] = {}
# _pending_plan: uid -> {"project_id", "owner", "repo", "branch", "plan", "snapshot"}
_pending_plan: dict[int, dict] = {}
# _last_change: uid -> {"project_id", "owner", "repo", "branch", "before_files": {path: str|None}}
_last_change: dict[int, dict] = {}


class AiDeveloper(StatesGroup):
    waiting_instruction = State()
    waiting_question = State()


def _fail(reason: str) -> str:
    return f"❌ {reason}"


async def _safe_edit(target: Message, text: str, **kwargs) -> Message:
    try:
        return await target.edit_text(text, **kwargs)
    except TelegramBadRequest:
        return await target.answer(text, **kwargs)


def _chunks(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


async def _send_long(msg: Message, text: str, reply_markup=None):
    parts = _chunks(text)
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await msg.answer(part, reply_markup=reply_markup if is_last else None)


async def _get_project_and_token(uid: int, pid: str) -> tuple[dict, str] | tuple[None, None]:
    project = await github_projects_db.get_project(uid, pid)
    if not project:
        return None, None
    cred = await github_projects_db.get_credential(uid)
    if not cred:
        return project, None
    token = github_crypto.decrypt_token(cred["encryptedToken"])
    return project, token


async def _get_snapshot(uid: int, project: dict, token: str, force: bool = False) -> dict | None:
    pid = str(project["_id"])
    cached = _snapshot_cache.get(uid)
    if not force and cached and cached["project_id"] == pid:
        return cached["snapshot"]
    snapshot = await ai_developer_service.fetch_repo_snapshot(
        token, project["githubOwner"], project["githubRepo"], project["defaultBranch"],
    )
    if snapshot:
        _snapshot_cache[uid] = {"project_id": pid, "snapshot": snapshot}
    return snapshot


# =========================================================
# Вхід у AI Developer (кнопка додана в handlers/github_deploy.py)
# =========================================================

@router.callback_query(F.data.startswith("aidev_open:"))
async def aidev_open(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    project, token = await _get_project_and_token(uid, pid)
    if not project:
        return await cb.answer("Проєкт не знайдено", show_alert=True)
    await cb.answer()
    if not token:
        return await cb.message.answer(
            "🔐 Спочатку підключи GitHub у «⚙️ Налаштування GitHub» — без нього AI Developer не має доступу до коду.",
        )
    if not ai_service.is_available():
        return await cb.message.answer("🤖 AI зараз недоступний (не налаштовано ключ на сервері).")

    text = (
        f"👨‍💻 *AI Developer* — {project['projectName']}\n"
        f"Repository: {project['githubOwner']}/{project['githubRepo']} "
        f"(гілка {project['defaultBranch']})\n\n"
        f"Обери дію:"
    )
    await cb.message.answer(text, reply_markup=ikb_ai_developer_menu(pid))


@router.callback_query(F.data.startswith("aidev_refresh:"))
async def aidev_refresh(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    project, token = await _get_project_and_token(uid, pid)
    if not project or not token:
        return await cb.answer("Сесія недоступна", show_alert=True)
    await cb.answer("🔄 Оновлюю...")
    wait = await cb.message.answer("⏳ Оновлюю дані з GitHub...")
    snapshot = await _get_snapshot(uid, project, token, force=True)
    if not snapshot:
        return await _safe_edit(wait, _fail("Не вдалося отримати вміст repository. Перевір доступ токена."))
    await _safe_edit(
        wait,
        f"✅ Оновлено. Прочитано {len(snapshot['files_text'])} файлів для AI-контексту "
        f"(з {len(snapshot['tree_paths'])} усього в репозиторії).",
        reply_markup=ikb_back_to_ai_menu(pid),
    )


# =========================================================
# Аналіз / баги / security / оптимізація — однакова "форма", різний промпт
# =========================================================

async def _run_readonly_ai_action(cb: CallbackQuery, pid: str, action_name: str, service_fn, wait_text: str):
    uid = cb.from_user.id
    project, token = await _get_project_and_token(uid, pid)
    if not project or not token:
        return await cb.answer("Сесія недоступна, відкрий проєкт заново", show_alert=True)

    allowed, remaining = await check_ai_limit(uid)
    if not allowed:
        await cb.answer()
        return await cb.message.answer(
            f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день). Спробуй завтра.",
        )

    await cb.answer()
    wait = await cb.message.answer(wait_text)
    snapshot = await _get_snapshot(uid, project, token)
    if not snapshot:
        return await _safe_edit(wait, _fail("Не вдалося прочитати repository. Перевір доступ токена."))

    result = await service_fn(snapshot)
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг сформувати відповідь. Спробуй ще раз пізніше."))

    await ai_usage_db.increment_usage(uid)
    try:
        await wait.delete()
    except Exception:
        pass
    await _send_long(cb.message, f"{action_name}\n\n{result}", reply_markup=ikb_back_to_ai_menu(pid))


@router.callback_query(F.data.startswith("aidev_analyze:"))
async def aidev_analyze(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await _run_readonly_ai_action(
        cb, pid, "🤖 *AI аналіз проєкту*", ai_developer_service.analyze_project,
        "⏳ Читаю репозиторій і аналізую...",
    )


@router.callback_query(F.data.startswith("aidev_bugs:"))
async def aidev_bugs(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await _run_readonly_ai_action(
        cb, pid, "🐛 *Знайдені потенційні баги*", ai_developer_service.find_bugs,
        "⏳ Шукаю баги в коді...",
    )


@router.callback_query(F.data.startswith("aidev_security:"))
async def aidev_security(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await _run_readonly_ai_action(
        cb, pid, "🔒 *Security Check*", ai_developer_service.security_check,
        "⏳ Перевіряю код на проблеми безпеки...",
    )


@router.callback_query(F.data.startswith("aidev_optimize:"))
async def aidev_optimize(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await _run_readonly_ai_action(
        cb, pid, "⚡ *Пропозиції оптимізації*", ai_developer_service.optimize_suggestions,
        "⏳ Шукаю можливості для покращення...",
    )


# =========================================================
# 💬 Запитати про код
# =========================================================

@router.callback_query(F.data.startswith("aidev_ask:"))
async def aidev_ask_start(cb: CallbackQuery, state: FSMContext):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    await state.set_state(AiDeveloper.waiting_question)
    await state.update_data(project_id=pid)
    await cb.message.answer("💬 Напиши питання про цей проєкт природною мовою:", reply_markup=kb_cancel())


@router.message(AiDeveloper.waiting_question, F.text == "❌ Скасувати")
async def aidev_ask_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(AiDeveloper.waiting_question)
async def aidev_ask_answer(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("project_id")
    await state.clear()
    uid = msg.from_user.id
    project, token = await _get_project_and_token(uid, pid) if pid else (None, None)
    if not project or not token:
        return await msg.answer("Сесія застаріла, відкрий проєкт заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Читаю код і формую відповідь...")
    snapshot = await _get_snapshot(uid, project, token)
    if not snapshot:
        return await _safe_edit(wait, _fail("Не вдалося прочитати repository."))

    answer = await ai_developer_service.ask_about_code(snapshot, msg.text or "")
    if not answer:
        return await _safe_edit(wait, _fail("AI не зміг відповісти. Спробуй переформулювати питання."))

    await ai_usage_db.increment_usage(uid)
    try:
        await wait.delete()
    except Exception:
        pass
    await _send_long(msg, f"💬 *Відповідь*\n\n{answer}", reply_markup=ikb_back_to_ai_menu(pid))


# =========================================================
# ✏️ Змінити код — головна фішка: опис → план → підтвердження → commit
# =========================================================

@router.callback_query(F.data.startswith("aidev_edit:"))
async def aidev_edit_start(cb: CallbackQuery, state: FSMContext):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    await state.set_state(AiDeveloper.waiting_instruction)
    await state.update_data(project_id=pid)
    await cb.message.answer(
        "✏️ Опиши, що потрібно змінити в проєкті. Наприклад:\n"
        "«Додай у бот кнопку для видалення задачі після підтвердження»",
        reply_markup=kb_cancel(),
    )


@router.message(AiDeveloper.waiting_instruction, F.text == "❌ Скасувати")
async def aidev_edit_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(AiDeveloper.waiting_instruction)
async def aidev_edit_plan(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("project_id")
    await state.clear()
    uid = msg.from_user.id
    project, token = await _get_project_and_token(uid, pid) if pid else (None, None)
    if not project or not token:
        return await msg.answer("Сесія застаріла, відкрий проєкт заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Аналізую репозиторій і готую план змін...")
    snapshot = await _get_snapshot(uid, project, token)
    if not snapshot:
        return await _safe_edit(wait, _fail("Не вдалося прочитати repository."))

    plan = await ai_developer_service.plan_code_change(snapshot, msg.text or "")
    if not plan:
        return await _safe_edit(wait, _fail(
            "AI не зміг сформувати конкретний план змін для цього завдання. "
            "Спробуй описати детальніше або звузити запит.",
        ))

    await ai_usage_db.increment_usage(uid)

    _pending_plan[uid] = {
        "project_id": pid,
        "owner": project["githubOwner"],
        "repo": project["githubRepo"],
        "branch": project["defaultBranch"],
        "plan": plan,
        "snapshot": snapshot,
    }

    files_lines = "\n".join(
        f"• {f['path']} ({'новий файл' if f['action'] == 'create' else 'зміна'})" for f in plan["files"]
    )
    text = (
        f"🔧 Знайдено {len(plan['files'])} файл(ів) для зміни.\n\n"
        f"Що буде зроблено:\n{plan['summary']}\n\n"
        f"Файли:\n{files_lines}\n\n"
        f"Commit message: `{plan['commit_message']}`"
    )
    await _safe_edit(wait, text, reply_markup=ikb_change_confirm(pid))


@router.callback_query(F.data.startswith("aidev_cancel_plan:"))
async def aidev_cancel_plan(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    _pending_plan.pop(cb.from_user.id, None)
    await cb.answer("Скасовано")
    await cb.message.answer("❌ Зміну скасовано.", reply_markup=ikb_back_to_ai_menu(pid))


@router.callback_query(F.data.startswith("aidev_apply:"))
async def aidev_apply(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    pending = _pending_plan.pop(uid, None)
    if not pending or pending["project_id"] != pid:
        return await cb.answer("Сесія застаріла", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Вношу зміни й створюю commit у GitHub...")

    project, token = await _get_project_and_token(uid, pid)
    if not project or not token:
        return await _safe_edit(wait, _fail("GitHub недоступний — перепідключи акаунт у налаштуваннях."))

    plan = pending["plan"]
    snapshot = pending["snapshot"]
    owner, repo, branch = pending["owner"], pending["repo"], pending["branch"]

    try:
        commit_sha = await ai_developer_service.apply_code_change(token, owner, repo, branch, plan)
    except Exception:
        logger.exception("AI Developer apply_code_change crashed for uid=%s repo=%s/%s", uid, owner, repo)
        return await _safe_edit(wait, _fail(
            "Помилка під час запису змін у GitHub. Перевір права токена (потрібен repo) і спробуй ще раз.",
        ))

    files_changed = [f["path"] for f in plan["files"]]
    before_files = {p: snapshot["files_text"].get(p) for p in files_changed}
    _last_change[uid] = {
        "project_id": pid, "owner": owner, "repo": repo, "branch": branch,
        "before_files": before_files,
    }

    await github_projects_db.record_ai_change(
        uid, pid, "edit", plan["summary"], commit_sha, plan["commit_message"], files_changed,
    )
    # Наступний "AI аналіз/баги/..." підтягне вже оновлений код.
    _snapshot_cache.pop(uid, None)

    await _safe_edit(
        wait,
        f"✅ *Зміни застосовано!*\n\n"
        f"Commit: `{commit_sha[:7]}`\n"
        f"Файлів змінено: {len(files_changed)}\n\n"
        f"https://github.com/{owner}/{repo}/commit/{commit_sha}",
        reply_markup=ikb_back_to_ai_menu(pid),
    )


# =========================================================
# 📜 Історія AI-змін
# =========================================================

@router.callback_query(F.data.startswith("aidev_history:"))
async def aidev_history(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    history = await github_projects_db.get_ai_change_history(uid, pid)
    await cb.answer()
    if not history:
        return await cb.message.answer("📭 Ще не було жодної AI-зміни в цьому проєкті.", reply_markup=ikb_back_to_ai_menu(pid))

    lines = ["📜 *Історія AI-змін*"]
    for h in history[:10]:
        at = (h.get("at") or "")[:16].replace("T", " ")
        short_sha = (h.get("commitSha") or "")[:7]
        files = ", ".join(h.get("filesChanged") or [])[:120]
        lines.append(
            f"\n🕐 {at}\nCommit: `{short_sha}`\n{h.get('summary', '')}\nФайли: {files}",
        )
    await _send_long(cb.message, "\n".join(lines), reply_markup=ikb_back_to_ai_menu(pid))


# =========================================================
# ↩️ Скасувати останню AI-зміну
# =========================================================

@router.callback_query(F.data.startswith("aidev_undo:"))
async def aidev_undo_ask(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    last = _last_change.get(uid)
    await cb.answer()
    if not last or last["project_id"] != pid:
        return await cb.message.answer(
            "↩️ Немає AI-зміни для скасування в цій сесії "
            "(або бот перезапускався після останньої зміни — тоді відкат недоступний).",
            reply_markup=ikb_back_to_ai_menu(pid),
        )
    files_list = "\n".join(f"• {p}" for p in last["before_files"])
    await cb.message.answer(
        f"↩️ Скасувати останню AI-зміну?\n\nЦе відновить попередній вміст файлів:\n{files_list}\n\n"
        f"(новостворені AI файли, якщо вони були, це НЕ видалить)",
        reply_markup=ikb_undo_confirm(pid),
    )


@router.callback_query(F.data.startswith("aidev_undo_confirm:"))
async def aidev_undo_confirm(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    last = _last_change.pop(uid, None)
    if not last or last["project_id"] != pid:
        return await cb.answer("Сесія застаріла", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Відновлюю попередній вміст файлів...")

    project, token = await _get_project_and_token(uid, pid)
    if not project or not token:
        return await _safe_edit(wait, _fail("GitHub недоступний — перепідключи акаунт у налаштуваннях."))

    try:
        commit_sha, skipped_new = await ai_developer_service.undo_change(
            token, last["owner"], last["repo"], last["branch"], last["before_files"],
        )
    except Exception:
        logger.exception("AI Developer undo_change crashed for uid=%s", uid)
        return await _safe_edit(wait, _fail("Помилка під час відкату змін у GitHub."))

    if not commit_sha:
        return await _safe_edit(wait, _fail(
            "Немає що відновлювати — усі файли тієї зміни були новостворені AI (їх undo не видаляє).",
        ))

    _snapshot_cache.pop(uid, None)
    text = f"✅ Зміну скасовано.\n\nRevert commit: `{commit_sha[:7]}`"
    if skipped_new:
        text += "\n\n⚠️ Ці новостворені файли НЕ видалено (undo їх не чіпає):\n" + "\n".join(f"• {p}" for p in skipped_new)
    await _safe_edit(wait, text, reply_markup=ikb_back_to_ai_menu(pid))