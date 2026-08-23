import logging

import aiohttp
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="nearby")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

NEARBY_CATEGORIES = {
    "pharmacy": ("💊 Аптека", "amenity", "pharmacy"),
    "shop": ("🛒 Магазин", "shop", "supermarket"),
    "fuel": ("⛽ Заправка", "amenity", "fuel"),
    "atm": ("🏧 Банкомат", "amenity", "atm"),
}


class Nearby(StatesGroup):
    waiting_address = State()


def _ikb_categories() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"nb:{key}")] for key, (label, _, _) in NEARBY_CATEGORIES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🗺 Що поруч")
async def nearby_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(Nearby.waiting_address)
    await msg.answer(
        "🗺 *Що поруч?*\n\nНапиши адресу або місце (напр. `Warszawa, Marszałkowska 10`), "
        "або надішли геолокацію.",
        reply_markup=kb_cancel(),
    )


async def _geocode(query: str) -> tuple[float, float] | None:
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "todo-list-telegram-bot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(NOMINATIM_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        logger.exception("Geocoding failed for query=%s", query)
        return None


@router.message(Nearby.waiting_address, F.location)
async def nearby_location(msg: Message, state: FSMContext):
    await state.update_data(lat=msg.location.latitude, lon=msg.location.longitude)
    await state.set_state(None)
    await msg.answer("Що шукаємо поруч?", reply_markup=_ikb_categories())


@router.message(Nearby.waiting_address)
async def nearby_address(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    coords = await _geocode(msg.text.strip())
    if not coords:
        return await msg.answer("🤔 Не знайшов таку адресу. Спробуй точніше або надішли геолокацію.")

    await state.update_data(lat=coords[0], lon=coords[1])
    await state.set_state(None)
    await msg.answer("✅ Місце знайдено. Що шукаємо поруч?", reply_markup=_ikb_categories())


@router.callback_query(F.data.startswith("nb:"))
async def nearby_category(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    entry = NEARBY_CATEGORIES.get(key)
    if not entry:
        return await cb.answer()
    label, tag_key, tag_val = entry

    fd = await state.get_data()
    lat, lon = fd.get("lat"), fd.get("lon")
    if lat is None or lon is None:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Локація втрачена, почни спочатку через «🗺 Що поруч».")

    await cb.answer("Шукаю...")
    await state.clear()

    query = f"""
[out:json][timeout:15];
node[{tag_key}={tag_val}](around:1500,{lat},{lon});
out body 15;
"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OVERPASS_URL, data={"data": query}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return await cb.message.edit_text("⚠️ Сервіс пошуку тимчасово недоступний.")
                data = await resp.json()
    except Exception:
        logger.exception("Overpass query failed")
        return await cb.message.edit_text("⚠️ Не вдалося отримати дані. Спробуй пізніше.")

    elements = data.get("elements", [])
    if not elements:
        return await cb.message.edit_text(f"{label}\n\n📭 Нічого не знайдено поруч (1.5 км).")

    lines = [f"{label} поруч:\n"]
    for el in elements[:10]:
        name = el.get("tags", {}).get("name", "Без назви")
        lines.append(f"• {name}")
    await cb.message.edit_text("\n".join(lines))
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())