import logging

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from database import github_projects as github_projects_db
from services import github_api, github_crypto, github_zip
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.github import (
    ikb_github_settings, ikb_disconnect_confirm, ikb_repo_choice, ikb_deploy_confirm,
    ikb_projects_list, ikb_project_actions, ikb_project_delete_confirm, ikb_edit_fields,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="github_deploy")

_pending_new: dict[int, dict] = {}
_pending_saved: dict[int, dict] = {}
_pending_saved_target: dict[int, str] = {}
_pending_repos: dict[int, list] = {}
_pending_add: dict[int, dict] = {}
_pending_edit: dict[int, dict] = {}


class GithubDeploy(StatesGroup):
    settings_waiting_token = State()
    new_waiting_zip = State()
    new_waiting_repo_url = State()
    add_waiting_url = State()
    add_waiting_name = State()
    saved_waiting_zip = State()
    edit_waiting_value = State()


def _md_escape(text: str) -> str:
    """Escape legacy-Markdown special chars so dynamic text (filenames, repo
    names, exception messages, e.g. 'node_modules') can't break entity parsing."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _fail_text(reason: str, hint: str) -> str:
    return (
        f"❌ Не вдалося задеплоїти\n\nПричина:\n{_md_escape(reason)}"
        f"\n\nЩо зробити:\n{_md_escape(hint)}"
    )


async def _safe_edit(target: Message, text: str, **kwargs) -> Message:
    try:
        return await target.edit_text(text, **kwargs)
    except TelegramBadRequest:
        return await target.answer(text, **kwargs)


@router.message(F.text == "📦 Новий проєкт")
async def gh_new_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    cred = await github_projects_db.get_credential(msg.from_user.id)
    if not cred:
        return await msg.answer(
            "🔐 GitHub ще не підключено.\n\nСпочатку перейди в «⚙️ Налаштування GitHub» і підключи акаунт.",
            reply_markup=kb_main(),
        )
    await state.set_state(GithubDeploy.new_waiting_zip)
    await msg.answer("📦 Надішли ZIP-архів проєкту.", reply_markup=kb_cancel())


@router.message(F.text == "📚 Мої проєкти")
async def gh_my_projects(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    projects = await github_projects_db.list_projects(msg.from_user.id)
    if not projects:
        return await msg.answer("📭 Ще немає збережених проєктів.", reply_markup=ikb_projects_list([]))
    lines = ["📚 *Мої проєкти*", ""]
    for i, p in enumerate(projects, 1):
        lines.append(f"{i}. 🟢 {p['projectName']}\n   GitHub: {p['githubOwner']}/{p['githubRepo']}")
    await msg.answer("\n".join(lines), reply_markup=ikb_projects_list(projects))


@router.message(F.text == "⚙️ Налаштування GitHub")
async def gh_settings(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    cred = await github_projects_db.get_credential(msg.from_user.id)
    text = "⚙️ *Налаштування GitHub*\n\n"
    text += f"✅ Підключено як *{cred['githubLogin']}*" if cred else "❌ GitHub не підключено"
    await msg.answer(text, reply_markup=ikb_github_settings(connected=bool(cred)))


@router.callback_query(F.data == "ghset_connect")
async def gh_connect_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(GithubDeploy.settings_waiting_token)
    await cb.message.answer(
        "🔐 *Підключення GitHub*\n\n"
        "1. Відкрий https://github.com/settings/tokens/new\n"
        "2. Обери термін дії та права доступу *repo*\n"
        "3. Створи токен і встав його сюди повідомленням\n\n"
        "Я одразу видалю твоє повідомлення з токеном після обробки.",
        reply_markup=kb_cancel(),
    )


@router.message(GithubDeploy.settings_waiting_token, F.text == "❌ Скасувати")
async def gh_connect_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.settings_waiting_token)
async def gh_connect_token(msg: Message, state: FSMContext, bot: Bot):
    token = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    if not token:
        return await msg.answer("Токен порожній. Спробуй ще раз або натисни «❌ Скасувати».")

    user_info = await github_api.verify_token(token)
    if not user_info:
        return await msg.answer(_fail_text(
            "Токен недійсний або немає доступу.",
            "Перевір токен на github.com/settings/tokens і надішли ще раз.",
        ))

    if not github_crypto.is_available():
        await state.clear()
        return await msg.answer(_fail_text(
            "Не налаштовано ключ шифрування на сервері.",
            "Звернись до адміністратора бота.",
        ), reply_markup=kb_main())

    encrypted = github_crypto.encrypt_token(token)
    await github_projects_db.save_credential(msg.from_user.id, encrypted, user_info.get("login", "?"))
    await state.clear()
    await msg.answer(f"✅ GitHub підключено як *{user_info.get('login', '?')}*", reply_markup=kb_main())


@router.callback_query(F.data == "ghset_check")
async def gh_check_connection(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    cred = await github_projects_db.get_credential(uid)
    if not cred:
        return await cb.message.answer("❌ GitHub не підключено.")
    token = github_crypto.decrypt_token(cred["encryptedToken"])
    user_info = await github_api.verify_token(token) if token else None
    if not user_info:
        return await cb.message.answer("⚠️ Токен недійсний або відкликаний. Підключи GitHub заново.")
    await cb.message.answer(f"✅ Підключено як *{user_info.get('login', '?')}* — доступ активний.")


@router.callback_query(F.data == "ghset_disconnect")
async def gh_disconnect_ask(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "Відключити GitHub? Збережені проєкти залишаться, але деплой стане недоступним без токена.",
        reply_markup=ikb_disconnect_confirm(),
    )


@router.callback_query(F.data == "ghset_disconnect_yes")
async def gh_disconnect_yes(cb: CallbackQuery):
    await github_projects_db.delete_credential(cb.from_user.id)
    await cb.answer("Відключено")
    await cb.message.answer("🔌 GitHub відключено.", reply_markup=kb_main())


@router.callback_query(F.data == "ghset_disconnect_no")
async def gh_disconnect_no(cb: CallbackQuery):
    await cb.answer("Скасовано")


@router.message(GithubDeploy.new_waiting_zip, F.text == "❌ Скасувати")
async def gh_new_zip_cancel(msg: Message, state: FSMContext):
    await state.clear()
    _pending_new.pop(msg.from_user.id, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.new_waiting_zip, F.document)
async def gh_new_zip_received(msg: Message, state: FSMContext, bot: Bot):
    doc = msg.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        return await msg.answer("⚠️ Це не ZIP-файл. Надішли архів з розширенням .zip")

    if doc.file_size and doc.file_size > github_zip.MAX_ZIP_SIZE:
        return await msg.answer(_fail_text(
            f"Архів завеликий: {github_zip.fmt_size(doc.file_size)} "
            f"(ліміт Telegram Bot API — {github_zip.MAX_ZIP_SIZE // (1024 * 1024)} МБ).",
            "Прибери node_modules/venv/build-папки з архіву і спробуй ще раз.",
        ))

    wait = await msg.answer("⏳ Завантажую ZIP...")
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(tg_file.file_path)
        zip_bytes = buf.read()
    except TelegramBadRequest as e:
        logger.warning("Telegram refused zip download for uid=%s: %s", msg.from_user.id, e)
        return await _safe_edit(wait, _fail_text(
            "Telegram відмовився віддати файл (ймовірно, він завеликий).",
            f"Ліміт — {github_zip.MAX_ZIP_SIZE // (1024 * 1024)} МБ. Зменш архів і спробуй ще раз.",
        ))
    except Exception:
        logger.exception("Failed to download zip from Telegram for uid=%s", msg.from_user.id)
        return await _safe_edit(wait, _fail_text(
            "Не вдалося завантажити файл із Telegram.",
            "Спробуй надіслати архів ще раз.",
        ))

    await _safe_edit(wait, "✅ ZIP отримано\n\n⏳ Перевіряю файли...")

    try:
        info = github_zip.extract_zip(zip_bytes)
    except github_zip.ZipValidationError as e:
        return await _safe_edit(wait, _fail_text(e.reason, e.hint))
    except Exception:
        logger.exception("Zip extraction crashed for uid=%s", msg.from_user.id)
        return await _safe_edit(wait, _fail_text("Не вдалося обробити архів.", "Перевір архів і спробуй ще раз."))

    project_name = (doc.file_name or "project").rsplit(".", 1)[0]
    _pending_new[msg.from_user.id] = {"files": info["files"], "info": info, "project_name": project_name}

    summary = (
        f"✅ Файли перевірено\n\n"
        f"📦 *{project_name}*\n"
        f"Файлів: {info['file_count']}\n"
        f"Розмір: {github_zip.fmt_size(info['total_size'])}\n"
        f"package.json: {'✅' if info['has_package_json'] else '—'}\n"
        f"README: {'✅' if info['has_readme'] else '—'}\n"
        f"Тип проєкту: {info['project_type']}"
    )
    await msg.answer(summary)

    cred = await github_projects_db.get_credential(msg.from_user.id)
    token = github_crypto.decrypt_token(cred["encryptedToken"]) if cred else None
    repos = await github_api.list_repos(token) if token else []
    await state.set_state(GithubDeploy.new_waiting_repo_url)
    if repos:
        _pending_repos[msg.from_user.id] = repos
        return await msg.answer("🔗 Вибери GitHub repository:", reply_markup=ikb_repo_choice(repos))
    await msg.answer(
        "🔗 Введи посилання на GitHub repository, наприклад:\nhttps://github.com/username/project",
        reply_markup=kb_cancel(),
    )


@router.message(GithubDeploy.new_waiting_zip)
async def gh_new_zip_wrong_type(msg: Message):
    await msg.answer("📦 Очікую ZIP-архів файлом (не текст).")


@router.message(GithubDeploy.new_waiting_repo_url, F.text == "❌ Скасувати")
async def gh_new_repo_cancel(msg: Message, state: FSMContext):
    await state.clear()
    _pending_new.pop(msg.from_user.id, None)
    _pending_repos.pop(msg.from_user.id, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


async def _resolve_repo_and_confirm(msg: Message, state: FSMContext, uid: int, owner: str, repo: str, kind: str):
    pending = _pending_new.get(uid) if kind == "new" else _pending_saved.get(uid)
    if not pending:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни спочатку.", reply_markup=kb_main())

    cred = await github_projects_db.get_credential(uid)
    if not cred:
        await state.clear()
        return await msg.answer("🔐 Спочатку підключи GitHub у «⚙️ Налаштування GitHub».", reply_markup=kb_main())
    token = github_crypto.decrypt_token(cred["encryptedToken"])

    repo_info = await github_api.get_repo(token, owner, repo) if token else None
    if not repo_info:
        return await msg.answer(_fail_text(
            f"Не знайшов repository {owner}/{repo} або немає доступу.",
            "Перевір посилання і права токена (потрібен доступ repo), спробуй ще раз.",
        ))

    if kind == "new":
        existing = await github_projects_db.find_by_repo(uid, repo_info["owner"], repo_info["name"])
        if existing:
            pending["project_id"] = str(existing["_id"])
            pending["project_name"] = existing["projectName"]

    pending["owner"] = repo_info["owner"]
    pending["repo"] = repo_info["name"]
    pending["default_branch"] = repo_info["default_branch"]
    pending["github_url"] = f"https://github.com/{repo_info['full_name']}"

    await state.clear()
    info = pending["info"]
    text = (
        f"🚀 *Готово до деплою*\n\n"
        f"Проєкт: {pending['project_name']}\n"
        f"Repository: {repo_info['full_name']}\n"
        f"Файлів: {info['file_count']}"
    )
    await msg.answer(text, reply_markup=ikb_deploy_confirm(kind))


@router.message(GithubDeploy.new_waiting_repo_url)
async def gh_new_repo_url(msg: Message, state: FSMContext):
    parsed = github_api.parse_repo_url((msg.text or "").strip())
    if not parsed:
        return await msg.answer("⚠️ Не розпізнав посилання. Формат: https://github.com/username/project")
    await _resolve_repo_and_confirm(msg, state, msg.from_user.id, parsed[0], parsed[1], kind="new")


@router.callback_query(F.data.startswith("ghrepo:"))
async def gh_repo_pick(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    repos = _pending_repos.get(uid) or []
    if idx >= len(repos):
        return await cb.answer("Застаріло", show_alert=True)
    repo = repos[idx]
    await cb.answer()
    await _resolve_repo_and_confirm(cb.message, state, uid, repo["owner"], repo["name"], kind="new")


@router.callback_query(F.data == "ghrepo_manual")
async def gh_repo_manual(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(GithubDeploy.new_waiting_repo_url)
    await cb.message.answer("🔗 Введи посилання на GitHub repository:", reply_markup=kb_cancel())


@router.callback_query(F.data.startswith("gh_deploy_no:"))
async def gh_deploy_cancel(cb: CallbackQuery):
    kind = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    (_pending_new if kind == "new" else _pending_saved).pop(uid, None)
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_main())


@router.callback_query(F.data.startswith("gh_deploy_yes:"))
async def gh_deploy_confirmed(cb: CallbackQuery):
    kind = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    pending = (_pending_new if kind == "new" else _pending_saved).pop(uid, None)
    if not pending:
        return await cb.answer("Сесія застаріла", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Підключаюсь до GitHub...")

    cred = await github_projects_db.get_credential(uid)
    if not cred:
        return await _safe_edit(wait, _fail_text(
            "GitHub не підключено.", "Підключи GitHub у налаштуваннях і спробуй ще раз.",
        ))
    token = github_crypto.decrypt_token(cred["encryptedToken"])
    if not token:
        return await _safe_edit(wait, _fail_text(
            "Не вдалося розшифрувати токен.", "Підключи GitHub заново в налаштуваннях.",
        ))

    files = pending["files"]
    total = len(files)

    async def progress(done, total_files):
        pct = int(done / total_files * 100)
        bar_len = 10
        filled = int(bar_len * done / total_files)
        bar = "█" * filled + "░" * (bar_len - filled)
        try:
            await wait.edit_text(f"⏳ Завантажую файли...\n{bar} {pct}%")
        except Exception:
            pass

    await _safe_edit(wait, "✅ GitHub підключено\n\n⏳ Завантажую файли...")

    commit_message = "Deploy update from Telegram"
    try:
        commit_sha = await github_api.deploy_files(
            token, pending["owner"], pending["repo"], pending["default_branch"],
            files, commit_message, progress_cb=progress,
        )
    except Exception:
        logger.exception("GitHub deploy crashed for uid=%s repo=%s/%s", uid, pending.get("owner"), pending.get("repo"))
        return await _safe_edit(wait, _fail_text(
            "Помилка під час завантаження файлів у GitHub.",
            "Перевір права токена (repo) і спробуй задеплоїти ще раз.",
        ))

    await _safe_edit(wait, "⏳ Створюю commit...")

    project_id = pending.get("project_id")
    if not project_id:
        doc = await github_projects_db.create_project(
            uid, pending["project_name"], pending["owner"], pending["repo"],
            pending["default_branch"], pending["github_url"],
        )
        project_id = doc.get("_id")
    if project_id:
        await github_projects_db.record_deploy(uid, project_id, commit_sha, commit_message, total)

    await _safe_edit(wait, f"✅ *DEPLOY ЗАВЕРШЕНО*\n\nRepository:\n{pending['github_url']}")


@router.callback_query(F.data.startswith("ghproj_deploy:"))
async def gh_proj_deploy_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    pid = cb.data.split(":", 1)[1]
    project = await github_projects_db.get_project(uid, pid)
    if not project:
        return await cb.answer("Проєкт не знайдено", show_alert=True)
    await cb.answer()
    _pending_saved_target[uid] = str(project["_id"])
    await state.set_state(GithubDeploy.saved_waiting_zip)
    await cb.message.answer(f"📦 Надішли ZIP-архів для оновлення «{project['projectName']}»", reply_markup=kb_cancel())


@router.message(GithubDeploy.saved_waiting_zip, F.text == "❌ Скасувати")
async def gh_saved_zip_cancel(msg: Message, state: FSMContext):
    await state.clear()
    _pending_saved_target.pop(msg.from_user.id, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.saved_waiting_zip, F.document)
async def gh_saved_zip_received(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    pid = _pending_saved_target.get(uid)
    project = await github_projects_db.get_project(uid, pid) if pid else None
    if not project:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни спочатку.", reply_markup=kb_main())

    doc = msg.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        return await msg.answer("⚠️ Це не ZIP-файл.")

    if doc.file_size and doc.file_size > github_zip.MAX_ZIP_SIZE:
        return await msg.answer(_fail_text(
            f"Архів завеликий: {github_zip.fmt_size(doc.file_size)} "
            f"(ліміт Telegram Bot API — {github_zip.MAX_ZIP_SIZE // (1024 * 1024)} МБ).",
            "Прибери node_modules/venv/build-папки з архіву і спробуй ще раз.",
        ))

    wait = await msg.answer("⏳ Завантажую ZIP...")
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(tg_file.file_path)
        zip_bytes = buf.read()
    except TelegramBadRequest as e:
        logger.warning("Telegram refused zip download for saved deploy uid=%s: %s", uid, e)
        return await _safe_edit(wait, _fail_text(
            "Telegram відмовився віддати файл (ймовірно, він завеликий).",
            f"Ліміт — {github_zip.MAX_ZIP_SIZE // (1024 * 1024)} МБ. Зменш архів і спробуй ще раз.",
        ))
    except Exception:
        logger.exception("Failed to download zip for saved deploy uid=%s", uid)
        return await _safe_edit(wait, _fail_text("Не вдалося завантажити файл із Telegram.", "Спробуй ще раз."))

    await _safe_edit(wait, "✅ ZIP отримано\n\n⏳ Перевіряю файли...")
    try:
        info = github_zip.extract_zip(zip_bytes)
    except github_zip.ZipValidationError as e:
        return await _safe_edit(wait, _fail_text(e.reason, e.hint))
    except Exception:
        logger.exception("Zip extraction crashed for uid=%s", uid)
        return await _safe_edit(wait, _fail_text("Не вдалося обробити архів.", "Перевір архів і спробуй ще раз."))

    await _safe_edit(
        wait,
        f"✅ Файли перевірено\n\n📦 *{project['projectName']}*\n"
        f"Repository: {project['githubOwner']}/{project['githubRepo']}\n\nЗамінити файли на GitHub?",
    )

    _pending_saved[uid] = {
        "files": info["files"], "info": info,
        "project_name": project["projectName"],
        "owner": project["githubOwner"], "repo": project["githubRepo"],
        "default_branch": project["defaultBranch"], "github_url": project["githubUrl"],
        "project_id": str(project["_id"]),
    }
    _pending_saved_target.pop(uid, None)
    await state.clear()
    await msg.answer("Підтверди дію:", reply_markup=ikb_deploy_confirm("saved"))


@router.message(GithubDeploy.saved_waiting_zip)
async def gh_saved_zip_wrong_type(msg: Message):
    await msg.answer("📦 Очікую ZIP-архів файлом (не текст).")


@router.callback_query(F.data.startswith("ghproj:"))
async def gh_proj_open(cb: CallbackQuery):
    uid = cb.from_user.id
    pid = cb.data.split(":", 1)[1]
    project = await github_projects_db.get_project(uid, pid)
    if not project:
        return await cb.answer("Проєкт не знайдено", show_alert=True)
    await cb.answer()
    last_deploy = project.get("lastDeployAt")
    text = (
        f"🟢 *{project['projectName']}*\n"
        f"GitHub: {project['githubOwner']}/{project['githubRepo']}\n"
        f"Останній деплой: {last_deploy[:16].replace('T', ' ') if last_deploy else 'ще не було'}"
    )
    await cb.message.answer(text, reply_markup=ikb_project_actions(project["_id"]))


@router.callback_query(F.data == "ghproj_back")
async def gh_proj_back(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    projects = await github_projects_db.list_projects(uid)
    await cb.message.answer("📚 Мої проєкти", reply_markup=ikb_projects_list(projects))


@router.callback_query(F.data == "ghproj_add")
async def gh_proj_add_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    cred = await github_projects_db.get_credential(cb.from_user.id)
    if not cred:
        return await cb.message.answer("🔐 Спочатку підключи GitHub у «⚙️ Налаштування GitHub».")
    await state.set_state(GithubDeploy.add_waiting_url)
    await cb.message.answer("🔗 Введи посилання на GitHub repository:", reply_markup=kb_cancel())


@router.message(GithubDeploy.add_waiting_url, F.text == "❌ Скасувати")
async def gh_proj_add_url_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.add_waiting_url)
async def gh_proj_add_url(msg: Message, state: FSMContext):
    parsed = github_api.parse_repo_url((msg.text or "").strip())
    if not parsed:
        return await msg.answer("⚠️ Не розпізнав посилання. Формат: https://github.com/username/project")

    cred = await github_projects_db.get_credential(msg.from_user.id)
    token = github_crypto.decrypt_token(cred["encryptedToken"]) if cred else None
    repo_info = await github_api.get_repo(token, parsed[0], parsed[1]) if token else None
    if not repo_info:
        return await msg.answer(_fail_text(
            f"Не знайшов repository {parsed[0]}/{parsed[1]} або немає доступу.",
            "Перевір посилання і спробуй ще раз.",
        ))

    _pending_add[msg.from_user.id] = repo_info
    await state.set_state(GithubDeploy.add_waiting_name)
    await msg.answer(f"Як назвати проєкт у боті? (за замовчуванням: {repo_info['name']})", reply_markup=kb_cancel())


@router.message(GithubDeploy.add_waiting_name, F.text == "❌ Скасувати")
async def gh_proj_add_name_cancel(msg: Message, state: FSMContext):
    _pending_add.pop(msg.from_user.id, None)
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.add_waiting_name)
async def gh_proj_add_name(msg: Message, state: FSMContext):
    repo_info = _pending_add.pop(msg.from_user.id, None)
    if not repo_info:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни спочатку.", reply_markup=kb_main())

    name = (msg.text or "").strip() or repo_info["name"]
    await github_projects_db.create_project(
        msg.from_user.id, name, repo_info["owner"], repo_info["name"],
        repo_info["default_branch"], f"https://github.com/{repo_info['full_name']}",
    )
    await state.clear()
    await msg.answer(f"✅ Проєкт «{name}» додано.", reply_markup=kb_main())


@router.callback_query(F.data.startswith("ghproj_history:"))
async def gh_proj_history(cb: CallbackQuery):
    uid = cb.from_user.id
    pid = cb.data.split(":", 1)[1]
    project = await github_projects_db.get_project(uid, pid)
    if not project:
        return await cb.answer("Проєкт не знайдено", show_alert=True)
    await cb.answer()
    history = project.get("deployHistory") or []
    if not history:
        return await cb.message.answer("📭 Ще не було деплоїв цього проєкту.")
    lines = [f"📜 *Історія — {project['projectName']}*"]
    for h in history[:10]:
        at = (h.get("at") or "")[:16].replace("T", " ")
        lines.append(f"\n🟢 {at}\n{h.get('fileCount', '?')} файлів\nCommit: {h.get('commit', '')}")
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("ghproj_del:"))
async def gh_proj_del_ask(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    await cb.message.answer(
        "Видалити проєкт зі списку в боті? GitHub repository НЕ буде видалено.",
        reply_markup=ikb_project_delete_confirm(pid),
    )


@router.callback_query(F.data.startswith("ghproj_del_yes:"))
async def gh_proj_del_yes(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    ok = await github_projects_db.delete_project(cb.from_user.id, pid)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        await cb.message.answer("🗑 Видалено зі списку проєктів.")


@router.callback_query(F.data.startswith("ghproj_del_no:"))
async def gh_proj_del_no(cb: CallbackQuery):
    await cb.answer("Скасовано")


@router.callback_query(F.data.startswith("ghproj_edit:"))
async def gh_proj_edit_menu(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    await cb.message.answer("Що редагувати?", reply_markup=ikb_edit_fields(pid))


@router.callback_query(F.data.startswith("ghedit_name:"))
async def gh_edit_name_start(cb: CallbackQuery, state: FSMContext):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    _pending_edit[cb.from_user.id] = {"project_id": pid, "field": "projectName"}
    await state.set_state(GithubDeploy.edit_waiting_value)
    await cb.message.answer("Нова назва проєкту:", reply_markup=kb_cancel())


@router.callback_query(F.data.startswith("ghedit_url:"))
async def gh_edit_url_start(cb: CallbackQuery, state: FSMContext):
    pid = cb.data.split(":", 1)[1]
    await cb.answer()
    _pending_edit[cb.from_user.id] = {"project_id": pid, "field": "githubUrl"}
    await state.set_state(GithubDeploy.edit_waiting_value)
    await cb.message.answer("Новий GitHub URL:", reply_markup=kb_cancel())


@router.message(GithubDeploy.edit_waiting_value, F.text == "❌ Скасувати")
async def gh_edit_cancel(msg: Message, state: FSMContext):
    _pending_edit.pop(msg.from_user.id, None)
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(GithubDeploy.edit_waiting_value)
async def gh_edit_value(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    pending = _pending_edit.pop(uid, None)
    if not pending:
        await state.clear()
        return await msg.answer("Сесія застаріла.", reply_markup=kb_main())

    value = (msg.text or "").strip()
    if pending["field"] == "githubUrl":
        parsed = github_api.parse_repo_url(value)
        if not parsed:
            _pending_edit[uid] = pending
            return await msg.answer("⚠️ Не розпізнав посилання. Формат: https://github.com/username/project")
        cred = await github_projects_db.get_credential(uid)
        token = github_crypto.decrypt_token(cred["encryptedToken"]) if cred else None
        repo_info = await github_api.get_repo(token, parsed[0], parsed[1]) if token else None
        if not repo_info:
            _pending_edit[uid] = pending
            return await msg.answer(_fail_text(
                f"Немає доступу до {parsed[0]}/{parsed[1]}.", "Перевір посилання і права токена.",
            ))
        updates = {
            "githubOwner": repo_info["owner"], "githubRepo": repo_info["name"],
            "defaultBranch": repo_info["default_branch"],
            "githubUrl": f"https://github.com/{repo_info['full_name']}",
        }
    else:
        updates = {pending["field"]: value}

    ok = await github_projects_db.update_project(uid, pending["project_id"], updates)
    await state.clear()
    await msg.answer("✅ Оновлено." if ok else "⚠️ Не вдалося оновити.", reply_markup=kb_main())