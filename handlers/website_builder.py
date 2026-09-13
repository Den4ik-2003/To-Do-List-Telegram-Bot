"""
НОВИЙ ФАЙЛ: handlers/website_builder.py

Хендлери фічі "🌐 AI Website Builder". Вхід — через звичайну reply-
категорію головного меню CATEGORY_WEBSITE (keyboards/main_menu.py), як і
решта фіч (GitHub, Магазини тощо), а НЕ через окреме inline-меню:
- "🔗 Клонувати сайт"  -> wb_clone_entry
- "🤖 Новий лендінг"    -> wb_scratch_entry
- "📂 Мої сайти"        -> wb_list_entry

Перемикання між категоріями (BACK_TO_MAIN, сам показ CATEGORY_WEBSITE)
обробляється загальним handlers/menu.py по MAIN_CATEGORIES — я його не
бачив, але судячи з main_menu.py він генеричний (проходиться по словнику
категорій), тож нову категорію додавати в нього окремо не треба.

Нічого не дублює:
- GitHub-токен: database.github_projects.get_credential() + github_crypto —
  ТОЙ САМИЙ, що й у Deploy ZIP / AI Developer.
- Запис у GitHub: services/github_api.deploy_files (як і скрізь), плюс
  ДВІ НОВІ функції create_repo/repo_exists — їх треба додати в
  services/github_api.py (окремий snippet, я не бачив цей файл цілком).
- Netlify: НОВИЙ ізольований services/netlify_service.py, спільний
  сервісний токен NETLIFY_TOKEN з налаштувань (за рішенням користувача —
  один спільний акаунт, а не токен на кожного).
- AI-ліміт: database/ai_usage.py + services/planner_service.check_ai_limit —
  той самий денний ліміт, що й у AI Developer / ранковому плані.

Модель стану:
- _pending[uid]: поточний "чернетковий" сайт (щойно згенерований або
  завантажений із "Мої сайти") — files, site_name, summary, і, якщо вже
  був задеплоєний раніше, github/netlify ідентифікатори. Живе в пам'яті
  процесу на час сесії.
- database/websites.py: ПОСТІЙНЕ збереження сайту (включно з files-
  знімком) відбувається одразу після ПЕРШОГО успішного деплою (GitHub або
  Netlify) — до першого деплою чернетка ніде, крім пам'яті, не зберігається.

⚠️ Роутер `router` (name="website_builder") потрібно зареєструвати в
main.py (dp.include_router(...)) — вже зроблено поруч з ai_developer.
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config.settings import AI_DAILY_LIMIT, NETLIFY_TOKEN
from database import ai_usage as ai_usage_db
from database import github_projects as github_projects_db
from database import websites as websites_db
from services import ai_service, github_crypto, github_api, netlify_service, website_builder_service
from services.planner_service import check_ai_limit
from keyboards.main_menu import kb_main, kb_cancel, kb_category, CATEGORY_WEBSITE
from keyboards.website_builder import ikb_wb_result, ikb_wb_sites_list

logger = logging.getLogger("tasks_bot")
router = Router(name="website_builder")

# _pending: uid -> {
#   "mode": "clone" | "scratch" | "refine",
#   "site_name": str, "summary": str, "commit_message": str,
#   "files": {path: content},
#   "db_id": str | None,               # якщо сайт уже колись збережено в БД
#   "github_owner": str | None, "github_repo": str | None, "branch": str | None,
#   "netlify_site_id": str | None, "netlify_url": str | None,
# }
_pending: dict[int, dict] = {}


class WebsiteBuilder(StatesGroup):
    waiting_clone_url = State()
    waiting_clone_description = State()
    waiting_landing_description = State()
    waiting_refine_instruction = State()


def _fail(reason: str) -> str:
    return f"❌ {reason}"


async def _safe_edit(target: Message, text: str, **kwargs) -> Message:
    try:
        return await target.edit_text(text, **kwargs)
    except TelegramBadRequest:
        return await target.answer(text, **kwargs)


async def _get_github_token(uid: int) -> str | None:
    cred = await github_projects_db.get_credential(uid)
    if not cred:
        return None
    return github_crypto.decrypt_token(cred["encryptedToken"])


def _result_text(pending: dict) -> str:
    files_list = "\n".join(f"• {p}" for p in pending["files"])
    lines = [
        f"🌐 *{pending['site_name']}*",
        "",
        pending["summary"],
        "",
        f"Файли:\n{files_list}",
    ]
    if pending.get("github_repo"):
        lines.append(f"\n📦 GitHub: {pending['github_owner']}/{pending['github_repo']}")
    if pending.get("netlify_url"):
        lines.append(f"🌍 Netlify: {pending['netlify_url']}")
    return "\n".join(lines)


# =========================================================
# Вхід — три кнопки з категорії "🌐 AI Website Builder"
# =========================================================

@router.message(F.text == "🔗 Клонувати сайт")
async def wb_clone_entry(msg: Message, state: FSMContext):
    await state.clear()
    if not ai_service.is_available():
        return await msg.answer("🤖 AI зараз недоступний (не налаштовано ключ на сервері).")
    await state.set_state(WebsiteBuilder.waiting_clone_url)
    await msg.answer(
        "🔗 Надішли посилання на сайт, стиль якого треба взяти за основу:",
        reply_markup=kb_cancel(),
    )


@router.message(F.text == "🤖 Новий лендінг")
async def wb_scratch_entry(msg: Message, state: FSMContext):
    await state.clear()
    if not ai_service.is_available():
        return await msg.answer("🤖 AI зараз недоступний (не налаштовано ключ на сервері).")
    await state.set_state(WebsiteBuilder.waiting_landing_description)
    await msg.answer(
        "🤖 Опиши, який лендінг потрібен.\n"
        "Наприклад: «Зроби лендинг для магазину чоловічих годинників у преміальному стилі».",
        reply_markup=kb_cancel(),
    )


@router.message(F.text == "📂 Мої сайти")
async def wb_list_entry(msg: Message, state: FSMContext):
    await state.clear()
    sites = await websites_db.get_user_websites(msg.from_user.id)
    if not sites:
        return await msg.answer("📭 Ще немає жодного збереженого сайту.")
    await msg.answer("📂 *Мої сайти*", reply_markup=ikb_wb_sites_list(sites))


# =========================================================
# 🔗 Клонувати та переробити сайт
# =========================================================

@router.message(WebsiteBuilder.waiting_clone_url, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_clone_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_landing_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_refine_instruction, F.text == "❌ Скасувати")
async def wb_cancel_flow(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(WebsiteBuilder.waiting_clone_url)
async def wb_clone_url_received(msg: Message, state: FSMContext):
    url = (msg.text or "").strip()
    if not url.startswith(("http://", "https://")):
        return await msg.answer("Це не схоже на посилання. Надішли повний URL (https://...):")
    await state.update_data(source_url=url)
    await state.set_state(WebsiteBuilder.waiting_clone_description)
    await msg.answer(
        "✏️ Тепер опиши свій продукт і побажання по стилю.\n"
        "Наприклад: «Візьми цей стиль і зроби магазин чоловічого одягу Athleon "
        "у чорному та білому кольорах».",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_clone_description)
async def wb_clone_generate(msg: Message, state: FSMContext):
    data = await state.get_data()
    url = data.get("source_url")
    await state.clear()
    uid = msg.from_user.id

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Аналізую сайт-референс і готую редизайн...")
    source_ref = await website_builder_service.fetch_source_reference(url)
    if not source_ref:
        return await _safe_edit(wait, _fail("Не вдалося завантажити сторінку за цим посиланням. Перевір URL і спробуй ще раз."))

    result = await website_builder_service.generate_clone_redesign(source_ref, msg.text or "")
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг сформувати сайт із цих даних. Спробуй описати детальніше."))

    await ai_usage_db.increment_usage(uid)
    _pending[uid] = {
        "mode": "clone", "db_id": None,
        "github_owner": None, "github_repo": None, "branch": None,
        "netlify_site_id": None, "netlify_url": None,
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False))


# =========================================================
# 🤖 Згенерувати лендінг з нуля
# =========================================================

@router.message(WebsiteBuilder.waiting_landing_description)
async def wb_scratch_generate(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Генерую лендінг (HTML/CSS/JS)...")
    result = await website_builder_service.generate_landing_from_scratch(msg.text or "")
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг сформувати сайт із цього опису. Спробуй описати детальніше."))

    await ai_usage_db.increment_usage(uid)
    _pending[uid] = {
        "mode": "scratch", "db_id": None,
        "github_owner": None, "github_repo": None, "branch": None,
        "netlify_site_id": None, "netlify_url": None,
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False))


# =========================================================
# 👀 Переглянути / 🚀 Deploy GitHub / 🌐 Deploy Netlify / ✏️ Переробити / ❌ Скасувати
# =========================================================

@router.callback_query(F.data == "wb_preview")
async def wb_preview(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)
    await cb.answer()
    for path, content in pending["files"].items():
        doc = BufferedInputFile(content.encode("utf-8"), filename=path.replace("/", "__"))
        await cb.message.answer_document(doc)


@router.callback_query(F.data == "wb_deploy_gh")
async def wb_deploy_gh(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)

    token = await _get_github_token(uid)
    if not token:
        await cb.answer()
        return await cb.message.answer(
            "🔐 Спочатку підключи GitHub у «⚙️ Налаштування GitHub» — без нього деплой у GitHub неможливий.",
        )

    await cb.answer()
    wait = await cb.message.answer("⏳ Деплою у GitHub...")

    try:
        if pending.get("github_repo"):
            owner, repo, branch = pending["github_owner"], pending["github_repo"], pending["branch"]
        else:
            base_slug = pending["site_name"] or "ai-website"
            repo_name = base_slug
            for suffix in range(2, 20):
                if not await github_api.repo_exists(token, cb.from_user.username or "me", repo_name):
                    break
                repo_name = f"{base_slug}-{suffix}"
            created = await github_api.create_repo(token, repo_name, private=True)
            if not created:
                return await _safe_edit(wait, _fail("Не вдалося створити репозиторій на GitHub. Перевір права токена (потрібен repo)."))
            owner, repo, branch = created["owner"], created["repo"], created["default_branch"]
            pending["github_owner"], pending["github_repo"], pending["branch"] = owner, repo, branch

        files_bytes = {p: c.encode("utf-8") for p, c in pending["files"].items()}
        commit_sha = await github_api.deploy_files(token, owner, repo, branch, files_bytes, pending["commit_message"])
    except Exception:
        logger.exception("Website Builder GitHub deploy crashed for uid=%s", uid)
        return await _safe_edit(wait, _fail("Помилка під час запису в GitHub. Спробуй ще раз пізніше."))

    await _persist_pending(uid, pending)

    await _safe_edit(
        wait,
        f"✅ Задеплоєно в GitHub!\nCommit: `{commit_sha[:7]}`\n"
        f"https://github.com/{owner}/{repo}",
    )
    await cb.message.answer(_result_text(pending), reply_markup=ikb_wb_result(True, bool(pending.get("netlify_url"))))


@router.callback_query(F.data == "wb_deploy_netlify")
async def wb_deploy_netlify(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)
    if not NETLIFY_TOKEN:
        await cb.answer()
        return await cb.message.answer("🌐 Netlify зараз недоступний (не налаштовано ключ на сервері).")

    await cb.answer()
    wait = await cb.message.answer("⏳ Деплою на Netlify...")

    try:
        if pending.get("netlify_site_id"):
            info = await netlify_service.redeploy_site(NETLIFY_TOKEN, pending["netlify_site_id"], pending["files"])
        else:
            info = await netlify_service.deploy_new_site(NETLIFY_TOKEN, pending["files"], desired_name=pending.get("site_name"))
    except Exception:
        logger.exception("Website Builder Netlify deploy crashed for uid=%s", uid)
        return await _safe_edit(wait, _fail("Помилка під час деплою на Netlify. Спробуй ще раз пізніше."))

    if not info or not info.get("url"):
        return await _safe_edit(wait, _fail("Не вдалося задеплоїти на Netlify. Спробуй ще раз пізніше."))

    pending["netlify_site_id"] = info["site_id"]
    pending["netlify_url"] = info["url"]
    await _persist_pending(uid, pending)

    await _safe_edit(wait, f"✅ Задеплоєно на Netlify!\n🌍 {info['url']}")
    await cb.message.answer(_result_text(pending), reply_markup=ikb_wb_result(bool(pending.get("github_repo")), True))


async def _persist_pending(uid: int, pending: dict) -> None:
    """Зберігає/оновлює запис у БД одразу після успішного деплою (GitHub
    або Netlify) — щоб сайт з'явився в «📂 Мої сайти» і його можна було
    переробляти навіть після рестарту бота."""
    payload = {
        "site_name": pending["site_name"],
        "summary": pending["summary"],
        "github_owner": pending.get("github_owner"),
        "github_repo": pending.get("github_repo"),
        "branch": pending.get("branch"),
        "netlify_site_id": pending.get("netlify_site_id"),
        "netlify_url": pending.get("netlify_url"),
        "files": pending["files"],
    }
    if pending.get("db_id"):
        await websites_db.update_website(uid, pending["db_id"], payload)
    else:
        pending["db_id"] = await websites_db.create_website(uid, payload)


@router.callback_query(F.data == "wb_cancel")
async def wb_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    _pending.pop(uid, None)
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_category(CATEGORY_WEBSITE))


# =========================================================
# ✏️ Переробити
# =========================================================

@router.callback_query(F.data == "wb_refine_start")
async def wb_refine_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid not in _pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)
    await cb.answer()
    await state.set_state(WebsiteBuilder.waiting_refine_instruction)
    await cb.message.answer(
        "✏️ Опиши, що змінити.\n"
        "Наприклад: «Зроби фон темнішим, додай блок відгуків і зроби кнопки більшими».",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_refine_instruction)
async def wb_refine_apply(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Вношу правки...")
    result = await website_builder_service.refine_site(pending["files"], msg.text or "")
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг застосувати ці правки. Спробуй переформулювати."))

    await ai_usage_db.increment_usage(uid)
    pending["files"] = result["files"]
    pending["summary"] = result["summary"]
    pending["commit_message"] = result["commit_message"]
    if result.get("site_name"):
        pending["site_name"] = result["site_name"]

    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(
        "✏️ Готово. Не забудь передеплоїти (GitHub/Netlify), щоб зміни стали видимими на сайті.\n\n"
        + _result_text(pending),
        reply_markup=ikb_wb_result(bool(pending.get("github_repo")), bool(pending.get("netlify_url"))),
    )


# =========================================================
# 📂 Мої сайти — відкриття конкретного сайту зі списку
# =========================================================

@router.callback_query(F.data.startswith("wb_site_open:"))
async def wb_site_open(cb: CallbackQuery):
    site_id = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    doc = await websites_db.get_website(uid, site_id)
    if not doc:
        return await cb.answer("Сайт не знайдено", show_alert=True)
    await cb.answer()

    _pending[uid] = {
        "mode": "refine", "db_id": doc["_id"],
        "site_name": doc.get("siteName"), "summary": doc.get("summary") or "",
        "commit_message": "AI website update",
        "files": doc.get("files", {}),
        "github_owner": doc.get("githubOwner"), "github_repo": doc.get("githubRepo"), "branch": doc.get("branch"),
        "netlify_site_id": doc.get("netlifySiteId"), "netlify_url": doc.get("netlifyUrl"),
    }
    await cb.message.answer(
        _result_text(_pending[uid]),
        reply_markup=ikb_wb_result(bool(doc.get("githubRepo")), bool(doc.get("netlifyUrl"))),
    )