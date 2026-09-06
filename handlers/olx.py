import logging
import re
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT, AI_LIMIT_TEXT
from config.settings import AI_DAILY_LIMIT
from database import olx as olx_db
from database import ai_usage as ai_usage_db
from services import olx_service
from services import olx_scanner
from services import ai_service
from services import resale_engine
from services import olx_audit
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="olx")

AI_EVAL_CACHE_HOURS = 12


class OlxListing(StatesGroup):
    waiting_url = State()


class OlxSearch(StatesGroup):
    waiting_title = State()
    waiting_price = State()
    waiting_location = State()
    waiting_radius = State()


class OlxCalc(StatesGroup):
    waiting_buy_price = State()
    waiting_delivery = State()
    waiting_repair = State()
    waiting_commission = State()
    waiting_sell_price = State()


class OlxBudget(StatesGroup):
    waiting_amount = State()


class OlxAudit(StatesGroup):
    waiting_url = State()


def _ikb_olx_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Стежити за оголошенням", callback_data="olx_add_listing")],
        [InlineKeyboardButton(text="🔥 Автопошук за критеріями", callback_data="olx_add_search")],
        [InlineKeyboardButton(text="📋 Мої підписки", callback_data="olx_list")],
        [InlineKeyboardButton(text="🧮 Калькулятор перепродажу", callback_data="olx_calc_start")],
        [
            InlineKeyboardButton(text="🏆 TOP Deals", callback_data="olx_top_deals"),
            InlineKeyboardButton(text="💰 Мій бюджет", callback_data="olx_budget_start"),
        ],
        [InlineKeyboardButton(text="🔍 Аудит мого оголошення", callback_data="olx_audit_start")],
    ])


def _ikb_after_add(tracker_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Resale Hunter: оцінити", callback_data=f"olx_analyze:{tracker_id}")],
    ])


def _ikb_after_analysis(tracker_id: str, status: str = "watching") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💬 Торг", callback_data=f"olx_negotiate:{tracker_id}"),
            InlineKeyboardButton(text="🧮 Перерахувати", callback_data=f"olx_reanalyze:{tracker_id}"),
        ],
        [
            InlineKeyboardButton(text="🔎 Схожі (AI)", callback_data=f"olx_similar:{tracker_id}"),
            InlineKeyboardButton(text="⭐ Зберегти", callback_data=f"olx_fav:{tracker_id}"),
        ],
    ]
    if status == "bought":
        rows.append([InlineKeyboardButton(text="📸 Створити оголошення", callback_data=f"olx_mkListing:{tracker_id}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Купив цей товар", callback_data=f"olx_bought:{tracker_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ikb_audit_actions(tracker_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Переписати опис", callback_data=f"olx_audit_desc:{tracker_id}"),
            InlineKeyboardButton(text="📸 Як перезняти фото", callback_data=f"olx_audit_photos:{tracker_id}"),
        ],
        [InlineKeyboardButton(text="💰 Оптимальна ціна", callback_data=f"olx_audit_price:{tracker_id}")],
        [InlineKeyboardButton(text="🚀 Покращити все", callback_data=f"olx_audit_all:{tracker_id}")],
        [InlineKeyboardButton(text="🔄 Оновити аудит", callback_data=f"olx_audit_redo:{tracker_id}")],
    ])


@router.message(F.text == "📉 OLX Ціни")
async def olx_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "📉 *OLX — AI Resale Hunter*\n\n"
        "Стеж за оголошенням і отримуй повний AI-аналіз вигідності перепродажу, "
        "перевіряй нові оголошення автоматично, або зроби аудит власного "
        "оголошення перед публікацією.",
        reply_markup=_ikb_olx_menu(),
    )


@router.callback_query(F.data == "olx_add_listing")
async def olx_add_listing_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxListing.waiting_url)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав посилання на оголошення OLX (https://www.olx.ua/d/uk/obyavlenie/...):",
        reply_markup=kb_cancel(),
    )


@router.message(OlxListing.waiting_url)
async def olx_add_listing_url(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    url = msg.text.strip()
    if "olx." not in url:
        return await msg.answer("⚠️ Схоже, це не посилання на OLX. Спробуй ще раз:")

    wait_msg = await msg.answer("🔎 Перевіряю оголошення...")
    details = await olx_service.fetch_listing_details(url)
    await state.clear()

    if not details or details.get("price") is None:
        return await wait_msg.edit_text(
            "🤔 Не вдалося зчитати ціну з цього оголошення. Перевір посилання або спробуй пізніше."
        )

    tracker_id = await olx_db.add_listing_tracker(
        msg.from_user.id, url,
        details["price"], details["currency"],
        title=details.get("title"),
        description=details.get("description"),
        location_text=details.get("location_text"),
        views=details.get("views"),
        photos_count=details.get("photos_count"),
        photos=details.get("photos"),
        params=details.get("params"),
    )

    extra_lines = []
    if details.get("location_text"):
        extra_lines.append(f"📍 {details['location_text']}")
    if details.get("views") is not None:
        extra_lines.append(f"👁 Переглядів: {details['views']}")
    if details.get("photos_count"):
        extra_lines.append(f"📷 Фото: {details['photos_count']}")
    extra_text = ("\n" + "\n".join(extra_lines) + "\n") if extra_lines else "\n"

    await wait_msg.edit_text(
        f"✅ Додано до стеження!\n\n"
        f"🏷 {details.get('title') or url}\n"
        f"💵 Поточна ціна: *{details['price']:.0f} {details['currency']}*"
        f"{extra_text}\n"
        f"Перевірятиму кожні кілька годин і повідомлю, якщо ціна впаде.",
        reply_markup=_ikb_after_add(tracker_id),
    )
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.callback_query(F.data == "olx_add_search")
async def olx_add_search_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxSearch.waiting_title)
    await cb.answer()
    await cb.message.answer(
        "🔥 *Автопошук OLX*\n\nЩо шукаємо? Напр.: `BMW 320d`",
        reply_markup=kb_cancel(),
    )


@router.message(OlxSearch.waiting_title)
async def olx_search_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(title_query=msg.text.strip())
    await state.set_state(OlxSearch.waiting_price)
    await msg.answer("💰 Максимальна ціна (в грн), або напиши «немає»:")


@router.message(OlxSearch.waiting_price)
async def olx_search_price(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    raw = msg.text.strip().lower()
    max_price = None
    if raw not in ("немає", "нема", "-"):
        try:
            max_price = float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            return await msg.answer("⚠️ Введи число (напр. 40000) або «немає»:")

    await state.update_data(max_price=max_price)
    await state.set_state(OlxSearch.waiting_location)
    await msg.answer("📍 Місто пошуку (напр. `Київ`), або «немає»:")


@router.message(OlxSearch.waiting_location)
async def olx_search_location(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    raw = msg.text.strip()
    location = "" if raw.lower() in ("немає", "нема", "-") else raw
    await state.update_data(location=location)

    if not location:
        return await _finish_search_tracker(msg, state, radius_km=0)

    await state.set_state(OlxSearch.waiting_radius)
    await msg.answer("📏 Радіус пошуку в км (напр. `100`):")


@router.message(OlxSearch.waiting_radius)
async def olx_search_radius(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    try:
        radius_km = int(msg.text.strip())
    except ValueError:
        return await msg.answer("⚠️ Введи ціле число, напр. `100`:")

    await _finish_search_tracker(msg, state, radius_km)


async def _finish_search_tracker(msg: Message, state: FSMContext, radius_km: int):
    fd = await state.get_data()
    title_query = fd.get("title_query", "")
    max_price = fd.get("max_price")
    location = fd.get("location", "")
    await state.clear()

    await olx_db.add_search_tracker(msg.from_user.id, title_query, max_price, location, radius_km)

    price_text = f"до {max_price:.0f} грн" if max_price else "без обмеження ціни"
    loc_text = f"{location} +{radius_km} км" if location else "без прив'язки до міста"
    await msg.answer(
        f"✅ Автопошук додано!\n\n"
        f"🔎 Запит: *{title_query}*\n"
        f"💰 {price_text}\n"
        f"📍 {loc_text}\n\n"
        f"Перевірятиму нові оголошення кожні кілька годин.",
        reply_markup=kb_main(),
    )


@router.callback_query(F.data == "olx_list")
async def olx_list_trackers(cb: CallbackQuery):
    trackers = await olx_db.get_user_trackers(cb.from_user.id)
    await cb.answer()

    if not trackers:
        return await cb.message.answer("📭 У тебе ще немає активних підписок OLX.")

    for t in trackers:
        tid = str(t["_id"])
        if t.get("type") == "listing":
            star = "⭐ " if t.get("favorited") else ""
            title = t.get("title") or t.get("url", "")[:50]
            line = f"🔗 {star}{title}\n💵 {t.get('last_price', '?')} {t.get('currency', '')}"
            if t.get("location_text"):
                line += f"\n📍 {t['location_text']}"
            if t.get("status") and t["status"] != "watching":
                line += f"\n📌 Статус: {t['status']}"
            rows = [
                [InlineKeyboardButton(text="🤖 AI Resale Hunter: оцінити", callback_data=f"olx_analyze:{tid}")],
                [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"olx_del:{tid}")],
            ]
        else:
            line = f"🔥 {t.get('title_query', '')} до {t.get('max_price') or '∞'} грн, {t.get('location') or 'будь-де'}"
            rows = [[InlineKeyboardButton(text="🗑 Видалити", callback_data=f"olx_del:{tid}")]]

        await cb.message.answer(line, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("olx_del:"))
async def olx_delete_tracker(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    ok = await olx_db.delete_tracker(tid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Підписку видалено.")
        except Exception:
            pass


# =========================================================
# AI RESALE HUNTER — повний аналіз оголошення
# =========================================================

def _listing_payload(tracker: dict) -> dict:
    return {
        "source": "olx.pl" if "olx.pl" in (tracker.get("url") or "") else "olx.ua",
        "url": tracker.get("url"),
        "title": tracker.get("title"),
        "price": tracker.get("last_price"),
        "currency": tracker.get("currency", "UAH"),
        "description": tracker.get("description"),
        "location_text": tracker.get("location_text"),
        "views": tracker.get("views"),
        "photos": tracker.get("photos") or [],
        "photos_count": tracker.get("photos_count"),
        "params": tracker.get("params") or [],
    }


async def _run_analysis(cb_or_msg, tracker: dict, tid: str, uid: int, force: bool = False):
    current_hash = olx_db.compute_content_hash(tracker)
    cached = tracker.get("resale_analysis")
    cached_at = tracker.get("resale_analysis_at")
    cached_hash = tracker.get("content_hash")

    if not force and cached and cached_at and cached_hash == current_hash:
        try:
            age_hours = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds() / 3600
        except ValueError:
            age_hours = AI_EVAL_CACHE_HOURS + 1
        if age_hours < AI_EVAL_CACHE_HOURS:
            return resale_engine.format_analysis(_listing_payload(tracker), cached, cached=True), None

    if not ai_service.is_available():
        return None, AI_ERROR_TEXT

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return None, AI_LIMIT_TEXT

    listing = _listing_payload(tracker)
    if len(listing["photos"]) <= 1:
        logger.info("OLX аналіз tracker=%s: обмежена кількість фото (%s)", tid, len(listing["photos"]))

    settings = await olx_db.get_user_settings(uid)
    analysis = await resale_engine.analyze_listing(listing, settings.get("min_margin_percent"))
    if not analysis:
        return None, AI_ERROR_TEXT

    await ai_usage_db.increment_usage(uid)
    await olx_db.save_resale_analysis(tid, analysis, current_hash)
    return resale_engine.format_analysis(listing, analysis, cached=False), None


@router.callback_query(F.data.startswith("olx_analyze:"))
async def olx_analyze_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    await cb.answer("Аналізую всі фото і дані оголошення...")
    wait_msg = await cb.message.answer("🤖 AI Resale Hunter аналізує оголошення (це може зайняти хвилину)...")

    text, error = await _run_analysis(cb, tracker, tid, uid, force=False)
    if error:
        return await wait_msg.edit_text(error)
    await wait_msg.edit_text(text, reply_markup=_ikb_after_analysis(tid, tracker.get("status", "watching")))


@router.callback_query(F.data.startswith("olx_reanalyze:"))
async def olx_reanalyze_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    await cb.answer("Оновлюю дані і перераховую...")
    wait_msg = await cb.message.answer("🔁 Тягну свіжі дані оголошення і перераховую...")

    fresh = await olx_service.fetch_listing_details(tracker["url"])
    if fresh and fresh.get("price") is not None:
        await olx_db.update_listing_price(tid, fresh["price"])
        tracker = {**tracker, **fresh}

    text, error = await _run_analysis(cb, tracker, tid, uid, force=True)
    if error:
        return await wait_msg.edit_text(error)
    await wait_msg.edit_text(text, reply_markup=_ikb_after_analysis(tid, tracker.get("status", "watching")))


@router.callback_query(F.data.startswith("olx_negotiate:"))
async def olx_negotiate_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    analysis = tracker.get("resale_analysis")
    if not analysis:
        await cb.answer()
        return await cb.message.answer("Спершу зроби AI-аналіз оголошення («AI Resale Hunter: оцінити»).")

    if not ai_service.is_available():
        await cb.answer()
        return await cb.message.answer(AI_ERROR_TEXT)

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        await cb.answer()
        return await cb.message.answer(AI_LIMIT_TEXT)

    await cb.answer("Генерую повідомлення...")
    wait_msg = await cb.message.answer("💬 Готую 3 варіанти повідомлення для торгу...")

    listing = _listing_payload(tracker)
    messages = await resale_engine.generate_negotiation_messages(listing, analysis)
    if not messages:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await olx_db.save_negotiation_messages(tid, messages)
    await wait_msg.edit_text(resale_engine.format_negotiation_messages(messages))


@router.callback_query(F.data.startswith("olx_bought:"))
async def olx_bought_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    await olx_db.set_tracker_status(tid, "bought")
    await cb.answer("Позначено як куплено ✅")

    try:
        await cb.message.edit_reply_markup(reply_markup=_ikb_after_analysis(tid, status="bought"))
    except Exception:
        pass

    await cb.message.answer(
        "✅ Позначив товар як куплений. Коли будеш готовий продавати — тисни "
        "«📸 Створити оголошення» на аналізі вище, і я згенерую готовий текст "
        "на основі попереднього AI-аналізу цього товару."
    )


# =========================================================
# 🔎 Знайти схожі
# =========================================================

_JUNK_CHARS_RE = re.compile(r"[\"'«»()\[\]{}]")
_NULLISH = {"null", "none", "невідомо", "не вказано", ""}

# Внутрішні артикули/SKU/ID товару типу "IG-1102110", "AB123456", "SKU-99"
# тощо. Вони НІКОЛИ не збігаються з реальними назвами товарів на OLX і, якщо
# потрапляють у пошуковий запит, змушують OLX повертати fallback-видачу
# (випадкові оголошення замість дійсно схожих).
_SKU_CODE_RE = re.compile(
    r"\b(?:[A-ZА-ЯІЇЄ]{1,5}-?\d{4,}|[A-ZА-ЯІЇЄ]{2,}\d{3,})\b", re.IGNORECASE
)


def _clean_query_text(text) -> str:
    if text is None:
        text = ""
    text = str(text)
    text = _JUNK_CHARS_RE.sub("", text)
    text = _SKU_CODE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_nonempty(*values) -> str:
    for v in values:
        cleaned = _clean_query_text(v)
        if cleaned and cleaned.lower() not in _NULLISH:
            return cleaned
    return ""


def _build_similar_query(analysis: dict, tracker: dict) -> str:
    if not isinstance(analysis, dict):
        analysis = {}

    brand = _first_nonempty(analysis.get("item_brand"), analysis.get("brand"))
    model = _first_nonempty(analysis.get("item_model"), analysis.get("model"))
    if brand and model:
        return f"{brand} {model}"
    if brand:
        return brand

    # item_name/name/title з AI-аналізу — пріоритетніші за "сирий" заголовок
    # оголошення, бо AI зазвичай вже дає нормалізовану людську назву товару
    # без внутрішніх артикулів продавця.
    name = _first_nonempty(analysis.get("item_name"), analysis.get("name"), analysis.get("title"))
    if not name:
        # tracker["title"] — останній fallback: заголовок реального оголошення
        # може містити артикул продавця (тому й проганяємо через ту саму
        # очистку від SKU-кодів у _clean_query_text/_first_nonempty).
        name = _first_nonempty(tracker.get("title"))

    words = name.split()
    return " ".join(words[:4])


@router.callback_query(F.data.startswith("olx_similar:"))
async def olx_similar_cb(cb: CallbackQuery):
    """
    🔎 Схожі (AI): бере той самий пошуковий запит, що й раніше (назва/бренд/
    модель товару з AI-аналізу або заголовка), але замість "сирого" списку
    результатів прожовує найдешевші свіжі оголошення через ТОЙ САМИЙ AI
    resale-аналіз, що й olx_scanner.scan_for_deals (🧲 Злови помилку), і
    показує їх відсортованими від найвигіднішого — тобто одразу видно не
    просто "схоже", а "схоже і вигідне для перепродажу".

    ВАЖЛИВО: як і "Злови помилку", кожна перевірка тут списує денний AI-ліміт
    користувача (по одному кредиту за кожне проаналізоване оголошення) — це
    свідомо, щоб не "з'їсти" ліміт непомітно однією кнопкою.
    """
    await cb.answer()

    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    wait_msg = await cb.message.answer(
        "🔎 Шукаю найдешевші схожі оголошення і прогоняю їх через AI Resale "
        "Hunter (це може зайняти хвилину)..."
    )

    try:
        tracker = await olx_db.get_tracker(tid)
        if not tracker or tracker.get("uid") != uid:
            return await wait_msg.edit_text("⚠️ Підписку не знайдено.")

        analysis = tracker.get("resale_analysis") or {}
        query_text = _build_similar_query(analysis, tracker)
        if not query_text:
            return await wait_msg.edit_text(
                "⚠️ Не вдалося визначити назву товару для пошуку (заголовок оголошення "
                "містить лише артикул/код без назви). Спробуй спершу зробити AI-аналіз "
                "(«🤖 AI Resale Hunter: оцінити»)."
            )

        domain = "olx.pl" if "olx.pl" in (tracker.get("url") or "") else "olx.ua"
        own_url = tracker.get("url")

        async def _progress(done: int, total: int):
            try:
                await wait_msg.edit_text(
                    f"🔎 Шукаю найдешевші схожі оголошення за «{query_text}»...\n"
                    f"🤖 AI перевірив {done}/{total}"
                )
            except Exception:
                pass

        ranked, error = await olx_scanner.scan_for_deals(
            uid, query_text, None, "", 0, domain=domain, progress_cb=_progress
        )

        if error == "ai_unavailable":
            return await wait_msg.edit_text(AI_ERROR_TEXT)
        if error == "ai_limit":
            return await wait_msg.edit_text(AI_LIMIT_TEXT)
        if error == "search_failed":
            return await wait_msg.edit_text(
                "⚠️ Не вдалося виконати пошук на OLX прямо зараз (сайт тимчасово "
                "заблокував запит або недоступний). Спробуй ще раз за хвилину."
            )

        ranked = [r for r in (ranked or []) if r.get("url") != own_url]

        if not ranked:
            return await wait_msg.edit_text(
                f"📭 Серед найдешевших свіжих оголошень за «{query_text}» AI не знайшов "
                f"нічого вигіднішого/вартого уваги. Спробуй пізніше — можливо, з'являться нові."
            )

        header = f"🔎 Найкращі схожі оголошення для перепродажу — «{query_text}»\n\n"

        # format_top_deals() за дизайном показує лише ТОП-3 (медалі), як і в
        # 🏆 TOP Deals, і не додає URL (бо там юзер вже має посилання у своїх
        # підписках). Тут юзер бачить ці оголошення вперше, тому окремо
        # додаємо посилання на ВСІ проаналізовані варіанти (не лише топ-3),
        # у тому ж форматі, що був у попередній версії "Схожих".
        top_summary = resale_engine.format_top_deals(ranked).replace("*", "")

        links_lines = ["", "🔗 Посилання на всі проаналізовані оголошення:"]
        for r in ranked:
            price = r.get("last_price")
            currency = r.get("currency", "UAH")
            price_text = f"{price:.0f} {currency}" if price is not None else "ціна не вказана"
            title = r.get("title") or "Без назви"
            url = r.get("url") or (r.get("_listing") or {}).get("url", "")
            links_lines.append(f"• {title} — {price_text}")
            if url:
                links_lines.append(f"  {url}")

        final_text = header + top_summary + "\n" + "\n".join(links_lines)

        # Назви й описи товарів приходять "сирими" з OLX і можуть містити
        # символи (*, _, [, ]), які ламають Markdown-парсинг Telegram —
        # надсилаємо без парсингу, щоб один "кривий" символ в одному
        # оголошенні не зривав усе повідомлення.
        await wait_msg.edit_text(final_text, parse_mode="")
    except Exception:
        logger.exception("olx_similar_cb failed for tracker=%s uid=%s", tid, uid)
        try:
            await wait_msg.edit_text("⚠️ Сталася технічна помилка під час пошуку схожих. Спробуй ще раз.")
        except Exception:
            pass


# =========================================================
# ⭐ Зберегти в обране
# =========================================================

@router.callback_query(F.data.startswith("olx_fav:"))
async def olx_favorite_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)

    new_val = not tracker.get("favorited", False)
    await olx_db.set_favorite(tid, new_val)
    await cb.answer("Збережено в обране ⭐" if new_val else "Прибрано з обраного")


# =========================================================
# 📸 Створити оголошення після покупки
# =========================================================

@router.callback_query(F.data.startswith("olx_mkListing:"))
async def olx_create_listing_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)

    analysis = tracker.get("resale_analysis")
    if not analysis:
        await cb.answer()
        return await cb.message.answer("⚠️ Спочатку потрібен AI-аналіз цього товару.")

    if not ai_service.is_available():
        await cb.answer()
        return await cb.message.answer(AI_ERROR_TEXT)

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        await cb.answer()
        return await cb.message.answer(AI_LIMIT_TEXT)

    await cb.answer("Готую оголошення...")
    wait_msg = await cb.message.answer("📸 Генерую оголошення для перепродажу...")

    listing = _listing_payload(tracker)
    data = await resale_engine.generate_resale_listing(listing, analysis)
    if not data:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await wait_msg.edit_text(resale_engine.format_resale_listing(data, listing.get("currency", "UAH")))


# =========================================================
# 🏆 TOP DEALS
# =========================================================

@router.callback_query(F.data == "olx_top_deals")
async def olx_top_deals_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()

    trackers = await olx_db.get_user_trackers(uid)
    listing_trackers = [t for t in trackers if t.get("type") == "listing"]
    ranked = resale_engine.rank_top_deals(listing_trackers)
    await cb.message.answer(resale_engine.format_top_deals(ranked))


# =========================================================
# 💰 Мій бюджет
# =========================================================

@router.callback_query(F.data == "olx_budget_start")
async def olx_budget_start_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxBudget.waiting_amount)
    await cb.answer()

    settings = await olx_db.get_user_settings(cb.from_user.id)
    current = settings.get("budget")
    current_text = f"\n\nПоточний бюджет: {current:.0f} грн" if current else ""
    await cb.message.answer(
        f"💰 Введи свій бюджет для закупівлі товарів на перепродаж (в грн):{current_text}",
        reply_markup=kb_cancel(),
    )


@router.message(OlxBudget.waiting_amount)
async def olx_budget_save(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    val = _parse_number(msg.text)
    if val is None or val <= 0:
        return await msg.answer("⚠️ Введи додатнє число, напр. `10000`:")

    await state.clear()
    uid = msg.from_user.id
    await olx_db.set_user_settings(uid, budget=val)

    trackers = await olx_db.get_user_trackers(uid)
    picks = resale_engine.recommend_purchases_within_budget(trackers, val)
    text = resale_engine.format_budget_recommendation(picks, val)
    await msg.answer(f"✅ Бюджет збережено: *{val:.0f} грн*\n\n{text}", reply_markup=kb_main())


# =========================================================
# КАЛЬКУЛЯТОР ПЕРЕПРОДАЖУ
# =========================================================

@router.callback_query(F.data == "olx_calc_start")
async def olx_calc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxCalc.waiting_buy_price)
    await cb.answer()
    await cb.message.answer("🧮 *Калькулятор перепродажу*\n\nЦіна покупки (грн):", reply_markup=kb_cancel())


def _parse_number(text: str) -> float | None:
    text = text.strip().lower()
    if text in ("немає", "нема", "-", "0"):
        return 0.0
    try:
        return float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


@router.message(OlxCalc.waiting_buy_price)
async def olx_calc_buy_price(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val is None:
        return await msg.answer("⚠️ Введи число, напр. `1500`:")
    await state.update_data(buy_price=val)
    await state.set_state(OlxCalc.waiting_delivery)
    await msg.answer("🚚 Витрати на доставку (грн), або «немає»:")


@router.message(OlxCalc.waiting_delivery)
async def olx_calc_delivery(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val is None:
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(delivery=val)
    await state.set_state(OlxCalc.waiting_repair)
    await msg.answer("🔧 Витрати на ремонт/чистку (грн), або «немає»:")


@router.message(OlxCalc.waiting_repair)
async def olx_calc_repair(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val is None:
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(repair=val)
    await state.set_state(OlxCalc.waiting_commission)
    await msg.answer("💳 Комісія майданчика продажу (%), або «немає»:")


@router.message(OlxCalc.waiting_commission)
async def olx_calc_commission(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val is None:
        return await msg.answer("⚠️ Введи число (наприклад 5) або «немає»:")
    await state.update_data(commission=val)
    await state.set_state(OlxCalc.waiting_sell_price)
    await msg.answer("💰 Планована ціна продажу (грн), або «немає» — якщо ще не знаєш:")


@router.message(OlxCalc.waiting_sell_price)
async def olx_calc_sell_price(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    raw = msg.text.strip().lower()
    sell_price = None
    if raw not in ("немає", "нема", "-"):
        sell_price = _parse_number(msg.text)
        if sell_price is None:
            return await msg.answer("⚠️ Введи число або «немає»:")

    fd = await state.get_data()
    await state.clear()

    calc = resale_engine.calculate_resale(
        buy_price=fd["buy_price"],
        delivery=fd["delivery"],
        repair=fd["repair"],
        commission_percent=fd["commission"],
        sell_price=sell_price,
    )
    await msg.answer(resale_engine.format_calculation(calc), reply_markup=kb_main())


# =========================================================
# 🔍 АУДИТ МОГО ОГОЛОШЕННЯ
# =========================================================

@router.callback_query(F.data == "olx_audit_start")
async def olx_audit_start_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxAudit.waiting_url)
    await cb.answer()
    await cb.message.answer(
        "🔍 *Аудит мого оголошення*\n\n"
        "Встав посилання на СВОЄ оголошення на OLX — проаналізую фото, заголовок, "
        "опис, ціну і дам конкретні поради, як покращити.",
        reply_markup=kb_cancel(),
    )


async def _run_audit_flow(target_msg: Message, uid: int, url: str, force: bool = False):
    """Спільна логіка для першого аудиту і для «🔄 Оновити аудит»."""
    if not ai_service.is_available():
        return await target_msg.answer(AI_ERROR_TEXT)
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await target_msg.answer(AI_LIMIT_TEXT)

    wait_msg = await target_msg.answer("🔎 Читаю оголошення...")
    details = await olx_service.fetch_listing_details(url)
    if not details or details.get("price") is None:
        return await wait_msg.edit_text(
            "🤔 Не вдалося зчитати оголошення (можливо, немає видимої ціни або воно "
            "видалене). Перевір посилання й спробуй ще раз."
        )

    tracker_id = await olx_db.upsert_own_listing(uid, url, details)
    tracker = await olx_db.get_tracker(tracker_id)
    current_hash = olx_db.compute_content_hash(tracker)

    if not force and tracker.get("audit_result") and tracker.get("content_hash") == current_hash:
        audit = tracker["audit_result"]
    else:
        await wait_msg.edit_text("🤖 Аналізую фото, заголовок, опис і ціну (може зайняти хвилину)...")
        audit = await olx_audit.audit_listing(details)
        if not audit:
            return await wait_msg.edit_text(AI_ERROR_TEXT)
        await ai_usage_db.increment_usage(uid)
        await olx_db.save_audit_result(tracker_id, audit, current_hash)

    report = olx_audit.format_audit_report(audit, details)
    await wait_msg.edit_text(report, reply_markup=_ikb_audit_actions(tracker_id))


@router.message(OlxAudit.waiting_url)
async def olx_audit_url_msg(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    url = msg.text.strip()
    if "olx." not in url:
        return await msg.answer("⚠️ Схоже, це не посилання на OLX. Спробуй ще раз:")

    await state.clear()
    await _run_audit_flow(msg, msg.from_user.id, url, force=False)
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.callback_query(F.data.startswith("olx_audit_redo:"))
async def olx_audit_redo_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)

    await cb.answer("Оновлюю аудит...")
    await _run_audit_flow(cb.message, uid, tracker["url"], force=True)


@router.callback_query(F.data.startswith("olx_audit_desc:"))
async def olx_audit_desc_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)
    audit = tracker.get("audit_result")
    if not audit:
        await cb.answer()
        return await cb.message.answer("⚠️ Спочатку зроби аудит цього оголошення.")
    await cb.answer()
    await cb.message.answer(olx_audit.format_description(audit))


@router.callback_query(F.data.startswith("olx_audit_photos:"))
async def olx_audit_photos_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)
    audit = tracker.get("audit_result")
    if not audit:
        await cb.answer()
        return await cb.message.answer("⚠️ Спочатку зроби аудит цього оголошення.")
    await cb.answer()
    await cb.message.answer(olx_audit.format_photo_advice(audit))


@router.callback_query(F.data.startswith("olx_audit_price:"))
async def olx_audit_price_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)
    audit = tracker.get("audit_result")
    if not audit:
        await cb.answer()
        return await cb.message.answer("⚠️ Спочатку зроби аудит цього оголошення.")
    await cb.answer()
    await cb.message.answer(olx_audit.format_price_advice(audit, _listing_payload(tracker)))


@router.callback_query(F.data.startswith("olx_audit_all:"))
async def olx_audit_all_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        return await cb.answer("Не знайдено", show_alert=True)
    audit = tracker.get("audit_result")
    if not audit:
        await cb.answer()
        return await cb.message.answer("⚠️ Спочатку зроби аудит цього оголошення.")
    await cb.answer()
    await cb.message.answer(olx_audit.format_full_improvement(audit, _listing_payload(tracker)))