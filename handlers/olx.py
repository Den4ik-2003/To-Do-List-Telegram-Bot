import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import olx as olx_db
from services import olx_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="olx")


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


@router.message(F.text == "📉 OLX Ціни")
async def olx_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "📉 *OLX — стеження за цінами*\n\n"
        "Стеж за конкретним оголошенням (скину, коли впаде ціна) або задай "
        "критерії й отримуй нові оголошення автоматично.",
        reply_markup=_ikb_olx_menu(),
    )


@router.callback_query(F.data == "olx_add_listing")
async def olx_add_listing_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OlxListing.waiting_url)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав посилання на оголошення OLX (https://www.olx.pl/d/oferta/...):",
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
    price_data = await olx_service.fetch_listing_price(url)
    await state.clear()

    if not price_data:
        return await wait_msg.edit_text(
            "🤔 Не вдалося зчитати ціну з цього оголошення. Перевір посилання або спробуй пізніше."
        )

    price, currency = price_data
    await olx_db.add_listing_tracker(msg.from_user.id, url, price, currency)
    await wait_msg.edit_text(
        f"✅ Додано до стеження!\n\n💵 Поточна ціна: *{price:.0f} {currency}*\n\n"
        f"Перевірятиму кожні кілька годин і повідомлю, якщо ціна впаде."
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
    await msg.answer("💰 Максимальна ціна (в PLN), або напиши «немає»:")


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
    await msg.answer("📍 Місто пошуку (напр. `Warszawa`), або «немає»:")


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

    price_text = f"до {max_price:.0f} PLN" if max_price else "без обмеження ціни"
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

    rows = []
    lines = ["📋 *Твої підписки OLX:*\n"]
    for t in trackers:
        tid = str(t["_id"])
        if t.get("type") == "listing":
            lines.append(f"🔗 {t.get('url', '')[:50]} — {t.get('last_price', '?')} {t.get('currency', '')}")
        else:
            lines.append(f"🔥 {t.get('title_query', '')} до {t.get('max_price') or '∞'} PLN, {t.get('location') or 'будь-де'}")
        rows.append([InlineKeyboardButton(text=f"🗑 {t.get('title_query') or t.get('url', '')[:25]}", callback_data=f"olx_del:{tid}")])

    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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