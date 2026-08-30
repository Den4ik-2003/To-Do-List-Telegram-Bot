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
from services import ai_service
from services import resale_engine
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="olx")

# Якщо AI-оцінку вже рахували менше AI_EVAL_CACHE_HOURS годин тому І
# оголошення не змінилось (порівнюємо content_hash) — беремо з кешу
# замість повторного (платного) AI-запиту.
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


# ДОДАНО: стан для введення бюджету (п.15 ТЗ)
class OlxBudget(StatesGroup):
    waiting_amount = State()


def _ikb_olx_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Стежити за оголошенням", callback_data="olx_add_listing")],
        [InlineKeyboardButton(text="🔥 Автопошук за критеріями", callback_data="olx_add_search")],
        [InlineKeyboardButton(text="📋 Мої підписки", callback_data="olx_list")],
        [InlineKeyboardButton(text="🧮 Калькулятор перепродажу", callback_data="olx_calc_start")],
        # ДОДАНО: TOP DEALS (п.14 ТЗ) і Бюджет (п.15 ТЗ)
        [
            InlineKeyboardButton(text="🏆 TOP Deals", callback_data="olx_top_deals"),
            InlineKeyboardButton(text="💰 Мій бюджет", callback_data="olx_budget_start"),
        ],
    ])


def _ikb_after_add(tracker_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Resale Hunter: оцінити", callback_data=f"olx_analyze:{tracker_id}")],
    ])


def _ikb_after_analysis(tracker_id: str, status: str = "watching") -> InlineKeyboardMarkup:
    """
    ЗМІНЕНО: додано кнопки "🔎 Схожі" та "⭐ Зберегти" (п.13, 17 ТЗ).
    Кнопка "✅ Купив" замінюється на "📸 Створити оголошення" після покупки,
    щоб не показувати неактуальну дію (п.16 ТЗ).
    """
    rows = [
        [
            InlineKeyboardButton(text="💬 Торг", callback_data=f"olx_negotiate:{tracker_id}"),
            InlineKeyboardButton(text="🧮 Перерахувати", callback_data=f"olx_reanalyze:{tracker_id}"),
        ],
        [
            InlineKeyboardButton(text="🔎 Схожі", callback_data=f"olx_similar:{tracker_id}"),
            InlineKeyboardButton(text="⭐ Зберегти", callback_data=f"olx_fav:{tracker_id}"),
        ],
    ]
    if status == "bought":
        rows.append([InlineKeyboardButton(text="📸 Створити оголошення", callback_data=f"olx_mkListing:{tracker_id}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Купив цей товар", callback_data=f"olx_bought:{tracker_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "📉 OLX Ціни")
async def olx_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "📉 *OLX — AI Resale Hunter*\n\n"
        "Стеж за оголошенням і отримуй повний AI-аналіз вигідності перепродажу: "
        "ринкова ціна, прибуток, ROI, готова стратегія торгу і ризики. "
        "Або задай критерії й отримуй нові оголошення автоматично.",
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
    """Приводить документ трекера до формату, який очікує resale_engine."""
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
    """Спільна логіка для olx_analyze та olx_reanalyze."""
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

    # На перерахунок примусово тягнемо свіжі дані з OLX (ціна/фото могли
    # змінитись) і форсуємо новий AI-запит, ігноруючи кеш.
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

    # ЗМІНЕНО: одразу перемальовуємо клавіатуру під аналізом, замінюючи
    # "✅ Купив" на "📸 Створити оголошення" (п.16 ТЗ) — без цього кнопка
    # створення оголошення була б доступна лише з нового повідомлення.
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
# ДОДАНО: 🔎 Знайти схожі (п.13 ТЗ)
# =========================================================

_JUNK_CHARS_RE = re.compile(r"[\"'«»()\[\]{}]")
_NULLISH = {"null", "none", "невідомо", "не вказано", ""}


def _clean_query_text(text: str) -> str:
    text = _JUNK_CHARS_RE.sub("", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _build_similar_query(analysis: dict, tracker: dict) -> str:
    """
    ЗМІНЕНО: раніше в пошук йшла повна AI-назва товару (item_name),
    яка часто виглядає як "Смартфон Xiaomi Redmi Note 12 128GB чорний,
    б/в, гарний стан" — на такий довгий і специфічний рядок пошук OLX
    майже завжди повертає 0 карток. Тепер запит будується коротко:
    спершу пробуємо "бренд + модель" (найточніше і найкоротше), і лише
    якщо їх немає — беремо перші кілька слів з item_name/title.
    """
    brand = _clean_query_text((analysis.get("item_brand") or "")).lower()
    model = _clean_query_text((analysis.get("item_model") or "")).lower()
    if brand not in _NULLISH and model not in _NULLISH:
        return _clean_query_text(f"{analysis.get('item_brand')} {analysis.get('item_model')}")

    name = _clean_query_text(analysis.get("item_name") or tracker.get("title") or "")
    words = name.split()
    return " ".join(words[:4])


@router.callback_query(F.data.startswith("olx_similar:"))
async def olx_similar_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    analysis = tracker.get("resale_analysis") or {}
    query_text = _build_similar_query(analysis, tracker)
    if not query_text:
        await cb.answer()
        return await cb.message.answer(
            "⚠️ Спочатку зроби AI-аналіз («🤖 AI Resale Hunter: оцінити»), щоб визначити назву товару для пошуку."
        )

    await cb.answer("Шукаю схожі оголошення...")
    domain = "olx.pl" if "olx.pl" in (tracker.get("url") or "") else "olx.ua"
    results = await olx_service.search_listings(query_text, None, "", 0, domain=domain)

    # ЗМІНЕНО: search_listings тепер повертає None при технічному збої
    # запиту (403/timeout) і [] лише коли запит успішний, але карток
    # немає — раніше обидва випадки виглядали як "нічого не знайдено",
    # хоча причина могла бути в тому, що OLX просто заблокував запит.
    if results is None:
        return await cb.message.answer(
            "⚠️ Не вдалося виконати пошук на OLX прямо зараз (сайт тимчасово "
            "заблокував запит або недоступний). Спробуй ще раз за хвилину."
        )

    own_url = tracker.get("url")
    results = [r for r in results if r.get("url") != own_url][:5]

    if not results:
        return await cb.message.answer(
            f"📭 Схожих оголошень за «{query_text}» не знайдено. "
            f"Спробуй пізніше — можливо, зараз мало активних оголошень саме за такою назвою."
        )

    lines = [f"🔎 *Схожі оголошення* — «{query_text}»", ""]
    for r in results:
        price_text = f"{r['price']:.0f} {r['currency']}" if r.get("price") else "ціна не вказана"
        lines.append(f"• {r['title']} — {price_text}")
        lines.append(f"  {r['url']}")
    await cb.message.answer("\n".join(lines))


# =========================================================
# ДОДАНО: ⭐ Зберегти в обране (п.13, 17 ТЗ)
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
# ДОДАНО: 📸 Створити оголошення після покупки (п.16 ТЗ)
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
# ДОДАНО: 🏆 TOP DEALS (п.14 ТЗ)
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
# ДОДАНО: 💰 Мій бюджет (п.15 ТЗ)
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