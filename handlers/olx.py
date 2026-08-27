import logging
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
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="olx")

# Якщо AI-оцінку вже рахували менше AI_EVAL_CACHE_HOURS годин тому — беремо
# з кешу замість повторного (платного) запиту до AI.
AI_EVAL_CACHE_HOURS = 12


class OlxListing(StatesGroup):
    waiting_url = State()


class OlxSearch(StatesGroup):
    waiting_title = State()
    waiting_price = State()
    waiting_location = State()
    waiting_radius = State()


def _ikb_olx_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Стежити за оголошенням", callback_data="olx_add_listing")],
        [InlineKeyboardButton(text="🔥 Автопошук за критеріями", callback_data="olx_add_search")],
        [InlineKeyboardButton(text="📋 Мої підписки", callback_data="olx_list")],
    ])


def _ikb_after_add(tracker_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Чи вигідно для перепродажу?", callback_data=f"olx_analyze:{tracker_id}")],
    ])


@router.message(F.text == "📉 OLX Ціни")
async def olx_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "📉 *OLX — стеження за цінами*\n\n"
        "Стеж за конкретним оголошенням (скину, коли впаде ціна, і можу оцінити "
        "вигідність для перепродажу) або задай критерії й отримуй нові оголошення автоматично.",
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
    # ЗМІНЕНО: раніше тягнули лише ціну (fetch_listing_price), тепер одразу
    # зчитуємо повні деталі (опис, локацію, перегляди, характеристики) —
    # це і потрібні дані для подальшої AI-оцінки вигідності перепродажу,
    # і кориснішу інформацію для самого користувача одразу при додаванні.
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
            title = t.get("title") or t.get("url", "")[:50]
            line = f"🔗 {title}\n💵 {t.get('last_price', '?')} {t.get('currency', '')}"
            if t.get("location_text"):
                line += f"\n📍 {t['location_text']}"
            rows = [
                [InlineKeyboardButton(text="🤖 Чи вигідно для перепродажу?", callback_data=f"olx_analyze:{tid}")],
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
# AI-ОЦІНКА ВИГІДНОСТІ ДЛЯ ПЕРЕПРОДАЖУ
# =========================================================

def _build_resale_prompt(t: dict) -> str:
    title = t.get("title") or "(без назви)"
    price = t.get("last_price")
    currency = t.get("currency", "UAH")
    description = (t.get("description") or "")[:1200]
    location = t.get("location_text") or "не вказано"
    views = t.get("views")
    photos = t.get("photos_count")
    params_list = t.get("params") or []
    params_text = "\n".join(f"- {p}" for p in params_list) or "(не вказані окремо)"

    return f"""Ти — досвідчений перекупник, який оцінює оголошення на OLX перед покупкою для перепродажу.
Відповідай ЛИШЕ у форматі JSON, без пояснень поза ним:
{{
  "verdict": "вигідно" | "сумнівно" | "невигідно",
  "estimated_resale_price_min": число,
  "estimated_resale_price_max": число,
  "estimated_profit": число,
  "max_buy_price": число,
  "reasoning": "коротке пояснення українською, 3-5 речень",
  "risks": "коротко про ризики українською, 1-3 речення"
}}

Дані оголошення:
Назва: {title}
Ціна продавця: {price} {currency}
Локація: {location}
Переглядів: {views if views is not None else "невідомо"}
Кількість фото: {photos if photos is not None else "невідомо"}
Характеристики:
{params_text}

Опис від продавця:
{description or "(опис відсутній)"}

Оціни на основі типової ринкової вартості подібних товарів в Україні,
стану товару (судячи з опису й характеристик), і чи достатньо фото/опису
для довіри до оголошення (мало фото чи розмитий опис — це ризик перепродажу
"кота в мішку"). estimated_profit = (середина діапазону перепродажу) - ціна продавця.
max_buy_price — максимальна ціна, вище якої купівля вже невигідна з урахуванням
типової маржі перекупника ~15-25%."""


@router.callback_query(F.data.startswith("olx_analyze:"))
async def olx_analyze_cb(cb: CallbackQuery):
    tid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    tracker = await olx_db.get_tracker(tid)
    if not tracker or tracker.get("uid") != uid:
        await cb.answer()
        return await cb.message.answer("⚠️ Підписку не знайдено.")

    # Кеш: якщо оцінку вже рахували недавно — не витрачаємо AI-ліміт повторно.
    cached_eval = tracker.get("ai_evaluation")
    cached_at = tracker.get("ai_evaluation_at")
    if cached_eval and cached_at:
        try:
            age_hours = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds() / 3600
        except ValueError:
            age_hours = AI_EVAL_CACHE_HOURS + 1
        if age_hours < AI_EVAL_CACHE_HOURS:
            await cb.answer()
            return await cb.message.answer(_fmt_evaluation(tracker, cached_eval, cached=True))

    if not ai_service.is_available():
        await cb.answer()
        return await cb.message.answer(AI_ERROR_TEXT)

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        await cb.answer()
        return await cb.message.answer(AI_LIMIT_TEXT)

    await cb.answer("Аналізую...")
    wait_msg = await cb.message.answer("🤖 Оцінюю вигідність перепродажу...")

    prompt = _build_resale_prompt(tracker)
    evaluation = await ai_service.generate_json(prompt, temperature=0.4)

    if not evaluation:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    await ai_usage_db.increment_usage(uid)
    await olx_db.save_ai_evaluation(tid, evaluation)
    await wait_msg.edit_text(_fmt_evaluation(tracker, evaluation, cached=False))


def _fmt_evaluation(tracker: dict, ev: dict, cached: bool) -> str:
    verdict = ev.get("verdict", "невідомо")
    verdict_emoji = {"вигідно": "🟢", "сумнівно": "🟡", "невигідно": "🔴"}.get(verdict, "⚪️")
    currency = tracker.get("currency", "UAH")

    price_min = ev.get("estimated_resale_price_min")
    price_max = ev.get("estimated_resale_price_max")
    profit = ev.get("estimated_profit")
    max_buy = ev.get("max_buy_price")

    lines = [f"{verdict_emoji} *Оцінка: {verdict}*", ""]
    if price_min is not None and price_max is not None:
        lines.append(f"📈 Орієнтовна ціна перепродажу: *{price_min:.0f}–{price_max:.0f} {currency}*")
    if profit is not None:
        lines.append(f"💰 Орієнтовний прибуток: *{profit:.0f} {currency}*")
    if max_buy is not None:
        lines.append(f"🎯 Максимальна ціна купівлі: *{max_buy:.0f} {currency}*")
    lines.append("")
    if ev.get("reasoning"):
        lines.append(f"🧠 {ev['reasoning']}")
    if ev.get("risks"):
        lines.append(f"\n⚠️ *Ризики:* {ev['risks']}")
    if cached:
        lines.append(f"\n_Оцінка з кешу (менше {AI_EVAL_CACHE_HOURS} год тому)_")
    return "\n".join(lines)