"""
ЗМІНЕНИЙ ФАЙЛ: handlers/website_builder.py

Додано відносно попередньої версії (без зміни старої логіки клонування/
генерації/деплою/переробки/товарів/замовлень — вона 1:1 як була):

5. 🗑 Видалення сайту (НОВЕ):
   - wb_delete_start: кнопка "🗑 Видалити" в ikb_wb_result (з'являється
     лише якщо сайт уже має db_id). Показує підтвердження зі списком
     того, що саме буде видалено (GitHub-репо / Netlify-сайт / запис у
     боті).
   - wb_delete_cancel: скасування, повертає в меню без жодних дій.
   - wb_delete_confirm: видаляє GitHub-репозиторій (якщо був) через
     github_api.delete_repo, Netlify-сайт (якщо був) через
     netlify_service.delete_site, і сам запис з MongoDB через
     websites_db.delete_website. Якщо GitHub/Netlify видалення не
     вдалося (наприклад, токену бракує прав) — користувачу прямо
     повідомляється, що саме не вдалося видалити і де зробити це вручну,
     а запис з бота видаляється в будь-якому разі (щоб не лишався
     "мертвий" запис, на який більше нема способу натиснути в UI бота).
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config.settings import AI_DAILY_LIMIT, NETLIFY_TOKEN, ORDERS_DISPLAY_LIMIT
from database import ai_usage as ai_usage_db
from database import github_projects as github_projects_db
from database import websites as websites_db
from database import orders as orders_db
from services import ai_service, github_crypto, github_api, netlify_service
from services import website_builder_service, product_asset_service
from services.planner_service import check_ai_limit
from keyboards.main_menu import kb_main, kb_cancel, kb_category, CATEGORY_WEBSITE
from keyboards.website_builder import (
    ikb_wb_result, ikb_wb_sites_list, ikb_wb_product_confirm, ikb_wb_delete_confirm,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="website_builder")

# _pending: uid -> {..., "assets": {path: bytes}}  # assets — бінарні
# файли (фото товарів), окремо від текстових "files", щоб не ламати
# генератор/refine_site, які працюють лише з текстом.
_pending: dict[int, dict] = {}

# _pending_product: uid -> {
#   "site_uid_key": int,           # ключ у _pending, до якого додаємо товар
#   "image_bytes": bytes | None,
#   "caption": str,
#   "data": {"title","description","category","price_uah","missing"},
# }
_pending_product: dict[int, dict] = {}


class WebsiteBuilder(StatesGroup):
    waiting_clone_url = State()
    waiting_clone_description = State()
    waiting_landing_description = State()
    waiting_refine_instruction = State()
    waiting_product_photo = State()
    waiting_product_missing = State()


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


def _inject_site_id(files: dict[str, str], site_id: str) -> dict[str, str]:
    """Підміняє плейсхолдер __SITE_ID__ (services/website_builder_service.
    _ORDER_FORM_RULES) на реальний db_id. Безпечно викликати повторно —
    якщо плейсхолдера вже нема (заміна була раніше), рядки просто
    лишаться без змін."""
    return {p: c.replace("__SITE_ID__", site_id) for p, c in files.items()}


def _all_deploy_files(pending: dict) -> dict[str, str | bytes]:
    """Об'єднує текстові файли сайту з бінарними asset'ами (фото товарів)
    для передачі в netlify_service (яка тепер приймає str|bytes)."""
    combined: dict[str, str | bytes] = dict(pending["files"])
    combined.update(pending.get("assets", {}))
    return combined


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
# 🔗 Клонувати та переробити сайт (без змін по суті)
# =========================================================

@router.message(WebsiteBuilder.waiting_clone_url, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_clone_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_landing_description, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_refine_instruction, F.text == "❌ Скасувати")
@router.message(WebsiteBuilder.waiting_product_missing, F.text == "❌ Скасувати")
async def wb_cancel_flow(msg: Message, state: FSMContext):
    await state.clear()
    _pending_product.pop(msg.from_user.id, None)
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
        "assets": {},
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False, False))


# =========================================================
# 🤖 Згенерувати лендінг з нуля (без змін по суті)
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
        "assets": {},
        **result,
    }
    try:
        await wait.delete()
    except Exception:
        pass
    await msg.answer(_result_text(_pending[uid]), reply_markup=ikb_wb_result(False, False, False))


# =========================================================
# 👀 Переглянути / 🚀 Deploy GitHub / 🌐 Deploy Netlify / ✏️ Переробити
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
        files_bytes.update(pending.get("assets", {}))  # фото товарів разом з текстовими файлами
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

    combined_files = _all_deploy_files(pending)  # текст + фото товарів
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
    """Зберігає/оновлює запис у БД одразу після успішного деплою (GitHub
    або Netlify). Якщо це ПЕРШЕ збереження (db_id ще не було), одразу
    підміняє __SITE_ID__ у файлах на реальний db_id — форма замовлення
    (services/website_builder_service._ORDER_FORM_RULES) від цього
    моменту шле заявки на правильний webhook. Файли з підміненим site_id
    ще НЕ задеплоєні — користувачу варто передеплоїти ще раз; про це
    попереджає _result_text/повідомлення хендлера вище."""
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

    if is_first_save:
        pending["files"] = _inject_site_id(pending["files"], pending["db_id"])
        await websites_db.update_website(uid, pending["db_id"], {"files": pending["files"]})


@router.callback_query(F.data == "wb_cancel")
async def wb_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    _pending.pop(uid, None)
    _pending_product.pop(uid, None)
    await cb.answer("Скасовано")
    await cb.message.answer("Скасовано.", reply_markup=kb_category(CATEGORY_WEBSITE))


# =========================================================
# ✏️ Переробити (без змін по суті)
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
        reply_markup=ikb_wb_result(
            bool(pending.get("github_repo")), bool(pending.get("netlify_url")), bool(pending.get("db_id"))
        ),
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
        "assets": {},
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
    else:  # price_uah
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
        lines.append(
            f"🛒 {date}\n👤 {o.get('name', '—')}\n📞 {o.get('phone', '—')}\n"
            f"📦 {o.get('product', '—')}\n💬 {o.get('comment') or '—'}\n"
        )
    await cb.message.answer("\n".join(lines))


# =========================================================
# 🗑 Видалення сайту (НОВЕ)
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