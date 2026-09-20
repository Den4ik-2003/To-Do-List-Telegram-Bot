
import base64
import logging

import aiohttp
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config.settings import AI_DAILY_LIMIT, NETLIFY_TOKEN, ORDERS_DISPLAY_LIMIT
from database import ai_usage as ai_usage_db
from database import github_projects as github_projects_db
from database import websites as websites_db
from database import templates as templates_db
from database import orders as orders_db
from services import ai_service, github_crypto, github_api, netlify_service, github_zip
from services import website_builder_service, product_asset_service
from services.website_builder_service import CloneFetchError
from services.planner_service import check_ai_limit
from keyboards.main_menu import kb_main, kb_cancel, kb_category, CATEGORY_WEBSITE
from keyboards.website_builder import (
    ikb_wb_result, ikb_wb_sites_list, ikb_wb_product_confirm, ikb_wb_delete_confirm,
    ikb_wb_history, ikb_wb_bot_manage, kb_photo_done,
    ikb_wb_templates_list, ikb_wb_template_delete_confirm,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="website_builder")

_pending: dict[int, dict] = {}
_pending_product: dict[int, dict] = {}
_pending_clarify: dict[int, dict] = {}
_pending_photo: dict[int, dict] = {}
_pending_bot: dict[int, dict] = {}
_pending_template: dict[int, dict] = {}
_pending_save_template: dict[int, dict] = {}

MAX_QUALITY_FIX_ROUNDS = 1


class WebsiteBuilder(StatesGroup):
    waiting_clone_url = State()
    waiting_clone_description = State()
    waiting_landing_description = State()
    waiting_clarification = State()
    waiting_refine_instruction = State()
    waiting_product_photo = State()
    waiting_product_missing = State()
    waiting_photo_images = State()
    waiting_photo_description = State()
    waiting_bot_token = State()
    waiting_bot_chat_id = State()
    waiting_template_zip = State()
    waiting_template_images = State()
    waiting_template_description = State()
    waiting_template_save_name = State()


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
    if pending.get("checklist"):
        lines.append(f"\n📋 Пунктів ТЗ у чеклисті: {len(pending['checklist'])}")
    if pending.get("quality_fixed"):
        lines.append(f"🔧 Автоматично виправлено проблем якості: {pending['quality_fixed']}")
    if pending.get("db_id"):
        bot_status = "🔌 підключено" if pending.get("notify_bot_connected") else "🔕 не підключено (заявки йдуть напряму в цей бот)"
        lines.append(f"📨 Бот для замовлень: {bot_status}")
    return "\n".join(lines)


def _inject_site_id(files: dict[str, str], site_id: str) -> dict[str, str]:
    return {p: c.replace("__SITE_ID__", site_id) for p, c in files.items()}


def _all_deploy_files(pending: dict) -> dict[str, str | bytes]:
    combined: dict[str, str | bytes] = dict(pending["files"])
    combined.update(pending.get("assets", {}))
    return combined


async def _save_version_snapshot(uid: int, pending: dict) -> None:
    if not pending.get("db_id"):
        return
    await websites_db.save_version(uid, pending["db_id"], {
        "files": pending["files"],
        "summary": pending.get("summary", ""),
        "commit_message": pending.get("commit_message", ""),
    })


async def _apply_checklist_pipeline(uid: int, result: dict, checklist: list[str]) -> dict:
    await ai_usage_db.increment_usage(uid)
    if not checklist:
        return result
    missing = await website_builder_service.verify_checklist(result["files"], checklist)
    await ai_usage_db.increment_usage(uid)
    if not missing:
        return result
    fixed = await website_builder_service.fix_missing_requirements(result["files"], missing)
    if fixed:
        await ai_usage_db.increment_usage(uid)
        result["files"] = fixed["files"]
        result["summary"] = fixed["summary"]
        result["commit_message"] = fixed["commit_message"]
        if fixed.get("site_name"):
            result["site_name"] = fixed["site_name"]
    return result


async def _apply_quality_pipeline(uid: int, result: dict) -> dict:
    """Після генерації/правок автоматично шукає технічні проблеми
    (биті посилання, відсутній viewport, неробочі кнопки/форми тощо) і,
    якщо знайдено, одразу виправляє — так, щоб користувач отримував уже
    перевірений сайт, а не сирий код."""
    try:
        issues = await website_builder_service.run_quality_check(result["files"])
        await ai_usage_db.increment_usage(uid)
    except Exception:
        logger.exception("website_builder: quality check crashed for uid=%s", uid)
        return result

    if not issues:
        return result

    fixed = await website_builder_service.fix_quality_issues(result["files"], issues)
    if fixed:
        await ai_usage_db.increment_usage(uid)
        result["files"] = fixed["files"]
        result["summary"] = fixed["summary"]
        result["commit_message"] = fixed["commit_message"]
        if fixed.get("site_name"):
            result["site_name"] = fixed["site_name"]
        result["quality_fixed"] = len(issues)
    return result


# =========================================================
# Вхід
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


@router.message(F.text == "🖼 Сайт із фото")
async def wb_photo_entry(msg: Message, state: FSMContext):
    await state.clear()
    if not ai_service.is_available():
        return await msg.answer("🤖 AI зараз недоступний (не налаштовано ключ на сервері).")
    _pending_photo[msg.from_user.id] = {"images": []}
    await state.set_state(WebsiteBuilder.waiting_photo_images)
    await msg.answer(
        "🖼 Надішли одне або кілька фото/скріншотів сайту (по черзі).\n"
        "Коли завершиш — натисни «✅ Готово». Максимум 10 фото.",
        reply_markup=kb_photo_done(),
    )


@router.message(F.text == "📂 Мої сайти")
async def wb_list_entry(msg: Message, state: FSMContext):
    await state.clear()
    sites = await websites_db.get_user_websites(msg.from_user.id)
    if not sites:
        return await msg.answer("📭 Ще немає жодного збереженого сайту.")
    await msg.answer("📂 *Мої сайти*", reply_markup=ikb_wb_sites_list(sites))


# =========================================================
# 📦 Мій шаблон
# =========================================================

@router.message(F.text == "📦 Мій шаблон")
async def wb_template_entry(msg: Message, state: FSMContext):
    await state.clear()
    if not ai_service.is_available():
        return await msg.answer("🤖 AI зараз недоступний (не налаштовано ключ на сервері).")
    templates = await templates_db.get_user_templates(msg.from_user.id)
    if not templates:
        await state.set_state(WebsiteBuilder.waiting_template_zip)
        return await msg.answer(
            "📦 Ще немає жодного збереженого шаблону.\n\n"
            "Надішли ZIP-архів з HTML/CSS/JS проєкту, який хочеш використовувати як базу.",
            reply_markup=kb_cancel(),
        )
    await msg.answer("📦 *Мої шаблони*\n\nОбери шаблон або завантаж новий:", reply_markup=ikb_wb_templates_list(templates))


@router.callback_query(F.data == "wb_tpl_new")
async def wb_tpl_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(WebsiteBuilder.waiting_template_zip)
    await cb.message.answer("📦 Надішли ZIP-архів з HTML/CSS/JS проєкту.", reply_markup=kb_cancel())


@router.callback_query(F.data == "wb_tpl_close")
async def wb_tpl_close(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("wb_tpl_del_yes:"))
async def wb_tpl_del_yes(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    ok = await templates_db.delete_template(cb.from_user.id, tid)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.delete()
        except Exception:
            pass


@router.callback_query(F.data == "wb_tpl_del_no")
async def wb_tpl_del_no(cb: CallbackQuery):
    await cb.answer("Скасовано")


@router.callback_query(F.data.startswith("wb_tpl_del:"))
async def wb_tpl_del_ask(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    await cb.answer()
    await cb.message.answer("Видалити цей шаблон назавжди?", reply_markup=ikb_wb_template_delete_confirm(tid))


@router.message(WebsiteBuilder.waiting_template_zip, F.text == "❌ Скасувати")
async def wb_template_zip_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(WebsiteBuilder.waiting_template_zip, F.document)
async def wb_template_zip_received(msg: Message, state: FSMContext, bot):
    doc = msg.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        return await msg.answer("⚠️ Це не ZIP-файл. Надішли архів з розширенням .zip")

    if doc.file_size and doc.file_size > github_zip.MAX_ZIP_SIZE:
        return await msg.answer(_fail(
            f"Архів завеликий ({github_zip.fmt_size(doc.file_size)}, "
            f"ліміт — {github_zip.MAX_ZIP_SIZE // (1024 * 1024)} МБ)."
        ))

    wait = await msg.answer("⏳ Завантажую і перевіряю архів...")
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(tg_file.file_path)
        zip_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити ZIP шаблону для uid=%s", msg.from_user.id)
        return await _safe_edit(wait, _fail("Не вдалося завантажити файл із Telegram. Спробуй ще раз."))

    try:
        info = github_zip.extract_zip(zip_bytes)
    except github_zip.ZipValidationError as e:
        return await _safe_edit(wait, _fail(f"{e.reason} {e.hint}"))
    except Exception:
        logger.exception("Zip extraction crashed for template uid=%s", msg.from_user.id)
        return await _safe_edit(wait, _fail("Не вдалося обробити архів. Перевір архів і спробуй ще раз."))

    text_files: dict[str, str] = {}
    binary_files: dict[str, bytes] = {}
    for path, content in info["files"].items():
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in {"html", "htm", "css", "js", "json", "svg", "txt", "md"}:
            try:
                text_files[path] = content.decode("utf-8", errors="ignore")
            except Exception:
                continue
        else:
            binary_files[path] = content

    if not any(p.lower().endswith((".html", ".htm")) for p in text_files):
        return await _safe_edit(wait, _fail(
            "В архіві не знайдено жодного HTML-файлу. Перевір, що це проєкт сайту, і спробуй ще раз."
        ))

    project_name = (doc.file_name or "template").rsplit(".", 1)[0]
    _pending_template[msg.from_user.id] = {
        "files": text_files, "assets": binary_files, "images": [], "name": project_name,
    }
    await state.set_state(WebsiteBuilder.waiting_template_images)
    await _safe_edit(
        wait,
        f"✅ Шаблон «{project_name}» завантажено ({len(text_files)} текстових файлів, "
        f"{len(binary_files)} ресурсів).\n\n"
        "🖼 Можеш додатково надіслати скріншоти бажаного вигляду (необов'язково) або одразу "
        "натисни «✅ Готово», щоб перейти до опису змін.",
    )
    await msg.answer("Надішли фото або натисни «✅ Готово»:", reply_markup=kb_photo_done())


@router.message(WebsiteBuilder.waiting_template_zip)
async def wb_template_zip_wrong_type(msg: Message):
    await msg.answer("📦 Очікую ZIP-архів файлом (не текст).")


@router.message(WebsiteBuilder.waiting_template_images, F.photo)
async def wb_template_image_received(msg: Message, state: FSMContext, bot):
    uid = msg.from_user.id
    ctx = _pending_template.get(uid)
    if ctx is None:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())
    if len(ctx["images"]) >= 10:
        return await msg.answer("⚠️ Максимум 10 фото. Натисни «✅ Готово», щоб продовжити.")

    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити фото шаблону для uid=%s", uid)
        return await msg.answer("⚠️ Не вдалося завантажити фото. Спробуй ще раз.")

    ctx["images"].append(image_bytes)
    await msg.answer(f"📸 Додано ({len(ctx['images'])}/10). Надішли ще фото або натисни «✅ Готово».")


@router.message(WebsiteBuilder.waiting_template_images, F.text == "✅ Готово")
async def wb_template_images_done(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid not in _pending_template:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())
    await state.set_state(WebsiteBuilder.waiting_template_description)
    await msg.answer(
        "✏️ Опиши, що потрібно змінити в шаблоні (продукт, тексти, кольори, товари тощо) "
        "або напиши «-», щоб лишити шаблон майже як є.",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_template_images, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_template_description, F.text == "❌ Скасувати")
async def wb_template_flow_cancel(msg: Message, state: FSMContext):
    await state.clear()
    _pending_template.pop(msg.from_user.id, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(WebsiteBuilder.waiting_template_description)
async def wb_template_description_received(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    ctx = _pending_template.pop(uid, None)
    if not ctx:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    description = (msg.text or "").strip()
    description = "" if description == "-" else description
    checklist: list[str] = []

    if description:
        wait = await msg.answer("⏳ Аналізую побажання...")
        analysis = await website_builder_service.analyze_requirements(description)
        if analysis:
            await ai_usage_db.increment_usage(uid)
            checklist = analysis["checklist"]
    else:
        wait = await msg.answer("⏳ Адаптую шаблон...")

    data_uris = [f"data:image/jpeg;base64,{base64.b64encode(b).decode()}" for b in ctx.get("images", [])]
    result = await website_builder_service.generate_from_template(
        ctx["files"], description, checklist, data_uris or None,
    )
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг адаптувати шаблон. Спробуй описати зміни детальніше."))

    result = await _apply_checklist_pipeline(uid, result, checklist)
    result = await _apply_quality_pipeline(uid, result)

    _pending[uid] = {
        "mode": "template", "db_id": None,
        "github_owner": None, "github_repo": None, "branch": None,
        "netlify_site_id": None, "netlify_url": None,
        "assets": dict(ctx.get("assets", {})), "checklist": checklist, "notify_bot_connected": False,
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await wait.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False, False))


@router.callback_query(F.data.startswith("wb_tpl_use:"))
async def wb_tpl_use(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    tid = cb.data.split(":", 1)[1]
    tpl = await templates_db.get_template(uid, tid)
    if not tpl:
        return await cb.answer("Шаблон не знайдено", show_alert=True)
    await cb.answer()
    _pending_template[uid] = {"files": tpl.get("files", {}), "assets": {}, "images": [], "name": tpl.get("name", "template")}
    await state.set_state(WebsiteBuilder.waiting_template_images)
    await cb.message.answer(
        f"📦 Шаблон «{tpl.get('name')}» обрано.\n\n"
        "🖼 Можеш надіслати скріншоти бажаного вигляду (необов'язково) або натисни «✅ Готово».",
        reply_markup=kb_photo_done(),
    )


# =========================================================
# 💾 Зберегти сайт як шаблон
# =========================================================

@router.callback_query(F.data == "wb_save_as_template")
async def wb_save_as_template_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid not in _pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)
    await cb.answer()
    await state.set_state(WebsiteBuilder.waiting_template_save_name)
    await cb.message.answer("Як назвати цей шаблон?", reply_markup=kb_cancel())


@router.message(WebsiteBuilder.waiting_template_save_name, F.text == "❌ Скасувати")
async def wb_save_template_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(WebsiteBuilder.waiting_template_save_name)
async def wb_save_template_name(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    name = (msg.text or "").strip()[:60] or pending.get("site_name") or "template"
    await templates_db.create_template(uid, name, dict(pending["files"]))
    await msg.answer(f"✅ Шаблон «{name}» збережено. Знайти його можна в «📦 Мій шаблон».", reply_markup=kb_main())


# =========================================================
# Скасування
# =========================================================

@router.message(WebsiteBuilder.waiting_clone_url, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_clone_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_landing_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_clarification, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_refine_instruction, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_product_missing, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_photo_images, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_photo_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_bot_token, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_bot_chat_id, F.text == "❌ Скасувати")
async def wb_cancel_flow(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    _pending_product.pop(uid, None)
    _pending_clarify.pop(uid, None)
    _pending_photo.pop(uid, None)
    _pending_bot.pop(uid, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


# =========================================================
# 🔗 Клонувати
# =========================================================

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
async def wb_clone_description_received(msg: Message, state: FSMContext):
    data = await state.get_data()
    url = data.get("source_url")
    await _start_requirements_check(msg, state, msg.from_user.id, "clone", (msg.text or "").strip(), url)


# =========================================================
# 🤖 Лендінг з нуля
# =========================================================

@router.message(WebsiteBuilder.waiting_landing_description)
async def wb_scratch_description_received(msg: Message, state: FSMContext):
    await _start_requirements_check(msg, state, msg.from_user.id, "scratch", (msg.text or "").strip(), None)


# =========================================================
# Спільний конвеєр: аналіз ТЗ → чекліст → уточнення → генерація → перевірка
# =========================================================

async def _start_requirements_check(
    msg: Message, state: FSMContext, uid: int, mode: str, description: str, source_url: str | None,
) -> None:
    await state.clear()
    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("⏳ Аналізую ТЗ і формую чекліст вимог...")
    analysis = await website_builder_service.analyze_requirements(description)
    checklist: list[str] = []
    questions: list[str] = []
    if analysis:
        await ai_usage_db.increment_usage(uid)
        checklist = analysis["checklist"]
        questions = analysis["clarifying_questions"]

    if questions:
        _pending_clarify[uid] = {
            "mode": mode, "description": description, "source_url": source_url, "checklist": checklist,
        }
        await state.set_state(WebsiteBuilder.waiting_clarification)
        q_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        return await _safe_edit(
            wait,
            "🤔 Перед генерацією потрібні уточнення:\n\n" + q_text +
            "\n\nВідповідай одним повідомленням на всі пункти.",
        )

    await _generate_and_show(wait, uid, mode, description, source_url, checklist)


@router.message(WebsiteBuilder.waiting_clarification)
async def wb_clarification_received(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    ctx = _pending_clarify.pop(uid, None)
    if not ctx:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    combined_description = ctx["description"] + "\n\nУточнення від користувача:\n" + (msg.text or "").strip()
    wait = await msg.answer("⏳ Готую сайт з урахуванням уточнень...")

    if ctx["mode"] == "photo":
        await _generate_from_photos_and_show(wait, uid, ctx["images"], combined_description, ctx["checklist"])
    else:
        await _generate_and_show(wait, uid, ctx["mode"], combined_description, ctx["source_url"], ctx["checklist"])


async def _generate_and_show(
    wait: Message, uid: int, mode: str, description: str, source_url: str | None, checklist: list[str],
) -> None:
    warning_text = None
    if mode == "clone":
        try:
            source_ref = await website_builder_service.fetch_source_reference(source_url)
        except CloneFetchError as e:
            return await _safe_edit(wait, e.user_message)
        warning_text = website_builder_service.clone_warning_message(source_ref)
        result = await website_builder_service.generate_clone_redesign(source_ref, description, checklist)
    else:
        result = await website_builder_service.generate_landing_from_scratch(description, checklist)

    if not result:
        return await _safe_edit(wait, _fail("AI не зміг сформувати сайт із цих даних. Спробуй описати детальніше."))

    result = await _apply_checklist_pipeline(uid, result, checklist)
    result = await _apply_quality_pipeline(uid, result)

    _pending[uid] = {
        "mode": mode, "db_id": None,
        "github_owner": None, "github_repo": None, "branch": None,
        "netlify_site_id": None, "netlify_url": None,
        "assets": {}, "checklist": checklist, "notify_bot_connected": False,
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    if warning_text:
        await wait.answer(warning_text)
    await wait.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False, False))


# =========================================================
# 🖼 Генерація з фото
# =========================================================

@router.message(WebsiteBuilder.waiting_photo_images, F.photo)
async def wb_photo_image_received(msg: Message, state: FSMContext, bot):
    uid = msg.from_user.id
    ctx = _pending_photo.get(uid)
    if ctx is None:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())
    if len(ctx["images"]) >= 10:
        return await msg.answer("⚠️ Максимум 10 фото. Натисни «✅ Готово», щоб продовжити.")

    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити фото сайту (website builder) для uid=%s", uid)
        return await msg.answer("⚠️ Не вдалося завантажити фото. Спробуй ще раз.")

    ctx["images"].append(image_bytes)
    await msg.answer(f"📸 Додано ({len(ctx['images'])}/10). Надішли ще фото або натисни «✅ Готово».")


@router.message(WebsiteBuilder.waiting_photo_images, F.text == "✅ Готово")
async def wb_photo_images_done(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    ctx = _pending_photo.get(uid)
    if not ctx or not ctx["images"]:
        return await msg.answer("⚠️ Спочатку надішли хоча б одне фото.")
    await state.set_state(WebsiteBuilder.waiting_photo_description)
    await msg.answer(
        "✏️ Опиши додаткові побажання (тематика, зміни, мова тощо) або напиши «-», "
        "щоб відтворити сайт максимально близько до фото як є.",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_photo_description)
async def wb_photo_description_received(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    ctx = _pending_photo.pop(uid, None)
    if not ctx or not ctx["images"]:
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    description = (msg.text or "").strip()
    description = "" if description == "-" else description
    checklist: list[str] = []

    if description:
        wait = await msg.answer("⏳ Аналізую ТЗ і фото...")
        analysis = await website_builder_service.analyze_requirements(description)
        if analysis:
            await ai_usage_db.increment_usage(uid)
            checklist = analysis["checklist"]
            if analysis["clarifying_questions"]:
                _pending_clarify[uid] = {
                    "mode": "photo", "description": description, "source_url": None,
                    "checklist": checklist, "images": ctx["images"],
                }
                await state.set_state(WebsiteBuilder.waiting_clarification)
                q_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(analysis["clarifying_questions"]))
                return await _safe_edit(
                    wait,
                    "🤔 Перед генерацією потрібні уточнення:\n\n" + q_text +
                    "\n\nВідповідай одним повідомленням на всі пункти.",
                )
    else:
        wait = await msg.answer("⏳ Аналізую фото і готую сайт...")

    await _generate_from_photos_and_show(wait, uid, ctx["images"], description, checklist)


async def _generate_from_photos_and_show(
    wait: Message, uid: int, images: list[bytes], description: str, checklist: list[str],
) -> None:
    data_uris = [f"data:image/jpeg;base64,{base64.b64encode(b).decode()}" for b in images]
    result = await website_builder_service.generate_site_from_photos(data_uris, description, checklist)
    if not result:
        return await _safe_edit(wait, _fail("AI не зміг відтворити сайт із цих фото. Спробуй чіткіші скріншоти або додай опис."))

    result = await _apply_checklist_pipeline(uid, result, checklist)
    result = await _apply_quality_pipeline(uid, result)

    _pending[uid] = {
        "mode": "photo", "db_id": None,
        "github_owner": None, "github_repo": None, "branch": None,
        "netlify_site_id": None, "netlify_url": None,
        "assets": {}, "checklist": checklist, "notify_bot_connected": False,
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await wait.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False, False))


# =========================================================
# 👀 Переглянути / 🚀 Deploy GitHub / 🌐 Deploy Netlify
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
        files_bytes.update(pending.get("assets", {}))
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
    await cb.message.answer(
        _result_text(pending),
        reply_markup=ikb_wb_result(True, bool(pending.get("netlify_url")), bool(pending.get("db_id"))),
    )


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

    combined_files = _all_deploy_files(pending)
    try:
        if pending.get("netlify_site_id"):
            info = await netlify_service.redeploy_site(NETLIFY_TOKEN, pending["netlify_site_id"], combined_files)
        else:
            info = await netlify_service.deploy_new_site(NETLIFY_TOKEN, combined_files, desired_name=pending.get("site_name"))
    except Exception:
        logger.exception("Website Builder Netlify deploy crashed for uid=%s", uid)
        return await _safe_edit(wait, _fail("Помилка під час деплою на Netlify. Спробуй ще раз пізніше."))

    if not info or not info.get("url"):
        return await _safe_edit(wait, _fail("Не вдалося задеплоїти на Netlify. Спробуй ще раз пізніше."))

    pending["netlify_site_id"] = info["site_id"]
    pending["netlify_url"] = info["url"]
    await _persist_pending(uid, pending)

    await _safe_edit(wait, f"✅ Задеплоєно на Netlify!\n🌍 {info['url']}")
    await cb.message.answer(
        _result_text(pending),
        reply_markup=ikb_wb_result(bool(pending.get("github_repo")), True, bool(pending.get("db_id"))),
    )


async def _persist_pending(uid: int, pending: dict) -> None:
    is_first_save = not pending.get("db_id")

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
        if pending.get("checklist"):
            await websites_db.save_checklist(uid, pending["db_id"], pending["checklist"])

    if is_first_save:
        pending["files"] = _inject_site_id(pending["files"], pending["db_id"])
        await websites_db.update_website(uid, pending["db_id"], {"files": pending["files"]})


@router.callback_query(F.data == "wb_cancel")
async def wb_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    _pending.pop(uid, None)
    _pending_product.pop(uid, None)
    _pending_clarify.pop(uid, None)
    _pending_photo.pop(uid, None)
    _pending_bot.pop(uid, None)
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_category(CATEGORY_WEBSITE))


# =========================================================
# 🛠 Виправити / Редагувати сайт
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
    result = await _apply_quality_pipeline(uid, result)
    await _save_version_snapshot(uid, pending)

    pending["files"] = result["files"]
    pending["summary"] = result["summary"]
    pending["commit_message"] = result["commit_message"]
    if result.get("site_name"):
        pending["site_name"] = result["site_name"]
    if result.get("quality_fixed"):
        pending["quality_fixed"] = result["quality_fixed"]

    if pending.get("db_id"):
        await websites_db.update_website(uid, pending["db_id"], {
            "files": pending["files"], "summary": pending["summary"],
        })

    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(
        "✏️ Готово. Не забудь передеплоїти (GitHub/Netlify), щоб зміни стали видимими на сайті.\n\n"
        + _result_text(pending),
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), bool(pending.get("db_id"))
        ),
    )


# =========================================================
# 📂 Мої сайти — відкриття
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
        "assets": {},
        "checklist": doc.get("checklist", []),
        "notify_bot_connected": bool(doc.get("notifyBotToken") and doc.get("notifyChatId")),
    }
    await cb.message.answer(
        _result_text(_pending[uid]),
        reply_markup=ikb_wb_result(bool(doc.get("githubRepo")), bool(doc.get("netlifyUrl")), True),
    )


# =========================================================
# 🖼 Додавання товару через фото
# =========================================================

@router.callback_query(F.data == "wb_product_start")
async def wb_product_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Спочатку задеплой сайт хоч раз (GitHub або Netlify)", show_alert=True)

    _pending_product[uid] = {"caption": "", "image_bytes": None, "data": None}
    await cb.answer()
    await state.set_state(WebsiteBuilder.waiting_product_photo)
    await cb.message.answer(
        f"🖼 Надішли фото товару для сайту «{pending['site_name']}».\n"
        "У підписі до фото можеш одразу вказати назву/ціну/розміри тощо "
        "(напр. «Nike Tech Fleece, чорний, 2999 грн, розміри M/L/XL»).",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_product_photo, F.text == "❌ Скасувати")
async def wb_product_cancel_state(msg: Message, state: FSMContext):
    await state.clear()
    _pending_product.pop(msg.from_user.id, None)
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(WebsiteBuilder.waiting_product_photo, F.photo)
async def wb_product_photo_received(msg: Message, state: FSMContext, bot):
    uid = msg.from_user.id
    pending = _pending.get(uid)
    prod = _pending_product.get(uid)
    if not pending or prod is None:
        await state.clear()
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        await state.clear()
        _pending_product.pop(uid, None)
        return await msg.answer(f"📊 Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день).", reply_markup=kb_main())

    wait = await msg.answer("📷 Аналізую товар...")
    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити фото товару (website builder) для uid=%s", uid)
        await state.clear()
        _pending_product.pop(uid, None)
        await _safe_edit(wait, "⚠️ Не вдалося завантажити фото. Спробуй ще раз.")
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    caption = (msg.caption or msg.text or "").strip()
    data = await product_asset_service.parse_product_submission(image_bytes, caption)
    await ai_usage_db.increment_usage(uid)

    prod["image_bytes"] = image_bytes
    prod["caption"] = caption
    prod["data"] = data

    if data["missing"]:
        field = data["missing"][0]
        await state.set_state(WebsiteBuilder.waiting_product_missing)
        prompt = "Вкажи, будь ласка, назву товару:" if field == "title" else "Вкажи, будь ласка, ціну товару (в грн):"
        return await _safe_edit(wait, f"🤔 Не вистачає даних.\n{prompt}")

    await state.clear()
    await _show_product_confirmation(wait, data)


async def _show_product_confirmation(target: Message, data: dict) -> None:
    price_line = f"{data['price_uah']:.0f} грн" if data.get("price_uah") else "не вказано"
    text = (
        "🖼 *Перевір дані товару перед додаванням:*\n\n"
        f"📝 Назва: {data['title']}\n"
        f"📄 Опис: {data.get('description', '—')}\n"
        f"🏷 Категорія: {data.get('category', '—')}\n"
        f"💵 Ціна: {price_line}"
    )
    await _safe_edit(target, text, reply_markup=ikb_wb_product_confirm())


@router.message(WebsiteBuilder.waiting_product_missing, F.text)
async def wb_product_missing_field(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    prod = _pending_product.get(uid)
    if not prod or not prod.get("data"):
        await state.clear()
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    data = prod["data"]
    field = data["missing"][0]
    value = (msg.text or "").strip()

    if field == "title":
        data["title"] = value[:80]
    else:
        cleaned = value.replace(",", ".").replace("грн", "").replace("₴", "").strip()
        try:
            data["price_uah"] = float(cleaned)
        except ValueError:
            return await msg.answer("⚠️ Не розпізнав число. Напиши, будь ласка, лише суму (наприклад: 2999):")

    data["missing"] = data["missing"][1:]

    if data["missing"]:
        next_field = data["missing"][0]
        prompt = "Вкажи, будь ласка, назву товару:" if next_field == "title" else "Вкажи, будь ласка, ціну товару (в грн):"
        return await msg.answer(prompt)

    await state.clear()
    wait = await msg.answer("⏳ Готую попередній перегляд...")
    await _show_product_confirmation(wait, data)


@router.callback_query(F.data == "wb_product_cancel")
async def wb_product_confirm_cancel(cb: CallbackQuery):
    _pending_product.pop(cb.from_user.id, None)
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_category(CATEGORY_WEBSITE))


@router.callback_query(F.data == "wb_product_confirm")
async def wb_product_confirm(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    prod = _pending_product.get(uid)
    if not pending or not prod or not prod.get("data"):
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await cb.answer(f"Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день)", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Додаю товар на сайт...")

    data = prod["data"]
    optimized = product_asset_service.optimize_image(prod["image_bytes"])
    asset_path = product_asset_service.build_asset_path(data["title"], uid)

    result = await website_builder_service.generate_product_card_update(pending["files"], data, asset_path)
    if not result:
        _pending_product.pop(uid, None)
        return await _safe_edit(wait, _fail("AI не зміг додати картку товару. Спробуй ще раз."))

    await ai_usage_db.increment_usage(uid)
    await _save_version_snapshot(uid, pending)

    pending["files"] = result["files"]
    pending["summary"] = result["summary"]
    pending["commit_message"] = result["commit_message"]
    pending.setdefault("assets", {})[asset_path] = optimized

    await websites_db.update_website(uid, pending["db_id"], {
        "files": pending["files"],
        "summary": pending["summary"],
    })

    _pending_product.pop(uid, None)

    await _safe_edit(
        wait,
        f"✅ Товар «{data['title']}» додано!\n"
        f"🖼 Фото збережено як `{asset_path}` (буде закомічено при наступному деплої).\n\n"
        "Не забудь передеплоїти (GitHub/Netlify), щоб товар з'явився на сайті.",
    )
    await cb.message.answer(
        _result_text(pending),
        reply_markup=ikb_wb_result(bool(pending.get("github_repo")), bool(pending.get("netlify_url")), True),
    )


# =========================================================
# 📦 Замовлення
# =========================================================

@router.callback_query(F.data == "wb_orders_view")
async def wb_orders_view(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сайт ще не збережено", show_alert=True)

    await cb.answer()
    orders = await orders_db.get_orders_for_site(uid, pending["db_id"], limit=ORDERS_DISPLAY_LIMIT)
    if not orders:
        return await cb.message.answer(f"📭 Замовлень для «{pending['site_name']}» ще немає.")

    lines = [f"📦 *Замовлення* — {pending['site_name']}\n"]
    for o in orders:
        date = (o.get("created_at") or "")[:16].replace("T", " ")
        status = "✅ доставлено" if o.get("delivered") else "⚠️ не доставлено"
        lines.append(
            f"🛒 {date} ({status})\n👤 {o.get('name', '—')}\n📞 {o.get('phone', '—')}\n"
            f"📦 {o.get('product', '—')}\n💬 {o.get('comment') or '—'}\n"
        )
    await cb.message.answer("\n".join(lines))


# =========================================================
# 🕐 Історія версій
# =========================================================

@router.callback_query(F.data == "wb_history_view")
async def wb_history_view(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сайт ще не збережено", show_alert=True)

    versions = await websites_db.get_versions(uid, pending["db_id"])
    await cb.answer()
    if not versions:
        return await cb.message.answer("📭 Історія версій порожня — редагувань з попереднім збереженим станом ще не було.")

    await cb.message.answer(
        "🕐 *Історія версій* (натисни, щоб відновити):",
        reply_markup=ikb_wb_history(pending["db_id"], versions),
    )


@router.callback_query(F.data == "wb_history_close")
async def wb_history_close(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("wb_history_restore:"))
async def wb_history_restore(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    try:
        index = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer("Помилка", show_alert=True)

    versions = await websites_db.get_versions(uid, pending["db_id"])
    if index >= len(versions):
        return await cb.answer("Ця версія вже недоступна", show_alert=True)

    await cb.answer()
    await _save_version_snapshot(uid, pending)

    version = versions[index]
    pending["files"] = version["files"]
    pending["summary"] = version.get("summary", pending["summary"])
    pending["commit_message"] = version.get("commitMessage", "AI website update")

    await websites_db.update_website(uid, pending["db_id"], {
        "files": pending["files"], "summary": pending["summary"],
    })

    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "⏪ Версію відновлено. Не забудь передеплоїти (GitHub/Netlify), щоб зміни стали видимими на сайті.\n\n"
        + _result_text(pending),
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), True
        ),
    )


# =========================================================
# 🔍 Перевірка виконання ТЗ
# =========================================================

@router.callback_query(F.data == "wb_checklist_check")
async def wb_checklist_check(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending:
        return await cb.answer("Немає активного сайту, почни заново", show_alert=True)
    if not pending.get("checklist"):
        return await cb.answer("Для цього сайту немає збереженого чеклиста ТЗ", show_alert=True)

    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return await cb.answer(f"Вичерпано денний ліміт AI-запитів ({AI_DAILY_LIMIT}/день)", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Перевіряю виконання пунктів ТЗ...")

    missing = await website_builder_service.verify_checklist(pending["files"], pending["checklist"])
    await ai_usage_db.increment_usage(uid)

    if not missing:
        return await _safe_edit(wait, "✅ Усі пункти ТЗ реалізовано на сайті.")

    await _save_version_snapshot(uid, pending)
    fixed = await website_builder_service.fix_missing_requirements(pending["files"], missing)
    missing_list = "\n".join(f"• {m}" for m in missing)
    if not fixed:
        return await _safe_edit(wait, f"⚠️ Знайдено невиконані пункти, але AI не зміг їх доопрацювати:\n{missing_list}")

    await ai_usage_db.increment_usage(uid)
    pending["files"] = fixed["files"]
    pending["summary"] = fixed["summary"]
    pending["commit_message"] = fixed["commit_message"]

    if pending.get("db_id"):
        await websites_db.update_website(uid, pending["db_id"], {
            "files": pending["files"], "summary": pending["summary"],
        })

    try:
        await wait.delete()
    except Exception:
        pass
    await cb.message.answer(
        f"🔧 Доопрацьовано {len(missing)} пункт(ів) ТЗ, яких не вистачало:\n{missing_list}\n\n"
        "Не забудь передеплоїти (GitHub/Netlify).\n\n" + _result_text(pending),
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), bool(pending.get("db_id"))
        ),
    )


# =========================================================
# 📨 Бот для замовлень
# =========================================================

@router.callback_query(F.data == "wb_bot_manage")
async def wb_bot_manage(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Спочатку задеплой сайт хоч раз", show_alert=True)

    await cb.answer()
    connected = pending.get("notify_bot_connected", False)
    status = "🔌 Підключено" if connected else "🔕 Не підключено — заявки надходять напряму в цей бот"
    await cb.message.answer(
        f"📨 *Бот для отримання замовлень*\n\n{status}\n\n"
        "Можна підключити власного Telegram-бота (створеного через @BotFather), "
        "щоб замовлення з сайту приходили саме в нього, а не в цей бот.",
        reply_markup=ikb_wb_bot_manage(connected),
    )


@router.callback_query(F.data == "wb_bot_manage_close")
async def wb_bot_manage_close(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "wb_bot_connect_start")
async def wb_bot_connect_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid not in _pending or not _pending[uid].get("db_id"):
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)
    await cb.answer()
    _pending_bot[uid] = {}
    await state.set_state(WebsiteBuilder.waiting_bot_token)
    await cb.message.answer(
        "🔑 Надішли токен бота, отриманий від @BotFather.\n"
        "Токен зберігається на сервері в зашифрованому вигляді і ніколи не потрапляє у код сайту.",
        reply_markup=kb_cancel(),
    )


@router.message(WebsiteBuilder.waiting_bot_token)
async def wb_bot_token_received(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    token = (msg.text or "").strip()
    if ":" not in token or len(token) < 20:
        return await msg.answer("⚠️ Це не схоже на токен бота. Формат: 123456:AAExample... Спробуй ще раз:")

    wait = await msg.answer("⏳ Перевіряю токен...")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                data = await resp.json(content_type=None)
    except Exception:
        return await _safe_edit(wait, "⚠️ Не вдалося перевірити токен. Спробуй ще раз пізніше.")

    if not data.get("ok"):
        return await _safe_edit(wait, "❌ Токен недійсний. Перевір і надішли ще раз, або натисни «❌ Скасувати».")

    bot_username = data["result"].get("username", "")
    _pending_bot.setdefault(uid, {})["token"] = token
    _pending_bot[uid]["bot_username"] = bot_username
    await state.set_state(WebsiteBuilder.waiting_bot_chat_id)
    await _safe_edit(
        wait,
        f"✅ Бот @{bot_username} підтверджено.\n\n"
        f"Тепер напиши боту @{bot_username} команду /start (щоб він міг тобі писати), "
        "а потім надішли сюди свій Telegram chat_id (число). Дізнатись його можна, "
        "написавши боту @userinfobot.",
    )


@router.message(WebsiteBuilder.waiting_bot_chat_id)
async def wb_bot_chat_id_received(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    ctx = _pending_bot.pop(uid, None)
    pending = _pending.get(uid)
    if not ctx or not ctx.get("token") or not pending or not pending.get("db_id"):
        return await msg.answer("Сесія застаріла, почни заново.", reply_markup=kb_main())

    chat_id = (msg.text or "").strip()
    if not chat_id.lstrip("-").isdigit():
        return await msg.answer("⚠️ chat_id має бути числом. Спробуй ще раз:")

    encrypted = github_crypto.encrypt_token(ctx["token"])
    await websites_db.set_notification_bot(uid, pending["db_id"], encrypted, chat_id)
    pending["notify_bot_connected"] = True

    await msg.answer(
        f"✅ Бот @{ctx['bot_username']} підключено — замовлення тепер надходитимуть у нього.\n\n"
        + _result_text(pending),
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), True
        ),
    )


@router.callback_query(F.data == "wb_bot_disconnect")
async def wb_bot_disconnect(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    await websites_db.set_notification_bot(uid, pending["db_id"], None, None)
    pending["notify_bot_connected"] = False
    await cb.answer("Відключено")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "🔕 Бота відключено. Замовлення знову надходитимуть напряму в цей бот.\n\n" + _result_text(pending),
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), True
        ),
    )


# =========================================================
# 🗑 Видалення сайту
# =========================================================

@router.callback_query(F.data == "wb_delete_start")
async def wb_delete_start(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сайт ще не збережено, немає що видаляти", show_alert=True)

    await cb.answer()

    will_delete = []
    if pending.get("github_repo"):
        will_delete.append(f"📦 GitHub-репозиторій {pending['github_owner']}/{pending['github_repo']}")
    if pending.get("netlify_url"):
        will_delete.append(f"🌍 Netlify-сайт {pending['netlify_url']}")
    will_delete.append("🗂 Запис і всі дані сайту в боті")

    details = "\n".join(f"• {p}" for p in will_delete)
    await cb.message.answer(
        f"⚠️ Точно видалити «{pending['site_name']}» НАЗАВЖДИ?\n\n"
        f"Буде видалено:\n{details}\n\n"
        "Цю дію не можна скасувати.",
        reply_markup=ikb_wb_delete_confirm(),
    )


@router.callback_query(F.data == "wb_delete_cancel")
async def wb_delete_cancel(cb: CallbackQuery):
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано, сайт не видалено.", reply_markup=kb_category(CATEGORY_WEBSITE))


@router.callback_query(F.data == "wb_delete_confirm")
async def wb_delete_confirm(cb: CallbackQuery):
    uid = cb.from_user.id
    pending = _pending.get(uid)
    if not pending or not pending.get("db_id"):
        return await cb.answer("Сесія застаріла, почни заново", show_alert=True)

    await cb.answer()
    wait = await cb.message.answer("⏳ Видаляю сайт...")

    problems: list[str] = []

    if pending.get("github_repo"):
        token = await _get_github_token(uid)
        if not token:
            problems.append(
                f"🐙 GitHub: токен не підключено, репозиторій "
                f"{pending['github_owner']}/{pending['github_repo']} НЕ видалено — "
                "видали вручну на github.com"
            )
        else:
            ok = await github_api.delete_repo(token, pending["github_owner"], pending["github_repo"])
            if not ok:
                problems.append(
                    f"🐙 GitHub: не вдалося видалити {pending['github_owner']}/{pending['github_repo']} "
                    "(можливо, токену бракує прав delete_repo) — видали вручну на github.com"
                )

    if pending.get("netlify_site_id"):
        if not NETLIFY_TOKEN:
            problems.append(
                f"🌍 Netlify: сервіс недоступний, сайт {pending.get('netlify_url', '')} НЕ видалено — "
                "видали вручну на netlify.com"
            )
        else:
            ok = await netlify_service.delete_site(NETLIFY_TOKEN, pending["netlify_site_id"])
            if not ok:
                problems.append(
                    f"🌍 Netlify: не вдалося видалити сайт {pending.get('netlify_url', '')} — "
                    "видали вручну на netlify.com"
                )

    site_name = pending["site_name"]
    await websites_db.delete_website(uid, pending["db_id"])
    _pending.pop(uid, None)
    _pending_product.pop(uid, None)

    if problems:
        text = f"⚠️ «{site_name}» видалено з бота, але виникли проблеми:\n\n" + "\n".join(f"• {p}" for p in problems)
    else:
        text = f"✅ «{site_name}» видалено повністю — з GitHub, Netlify і бота."

    await _safe_edit(wait, text)
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_category(CATEGORY_WEBSITE))