import asyncio
import logging

import aiohttp
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="nearby")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Overpass-сервери (особливо overpass-api.de) з 2026 року стали жорсткіше
# фільтрувати запити, що виглядають "програмними" — без нормального
# User-Agent і Accept-заголовків повертають 406, навіть якщо сам запит
# коректний. Явно задаємо їх, щоб не потрапляти під цей фільтр.
OVERPASS_HEADERS = {
    "User-Agent": "todo-list-telegram-bot/1.0 (contact: telegram bot)",
    "Accept": "application/json",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
}

NEARBY_CATEGORIES = {
    "pharmacy": ("💊 Аптека", "amenity", "pharmacy"),
    "shop": ("🛒 Магазин", "shop", "supermarket"),
    "fuel": ("⛽ Заправка", "amenity", "fuel"),
    "atm": ("🏧 Банкомат", "amenity", "atm"),
}

# lat/lon зберігаємо ОКРЕМО від FSM-стану, а не в ньому — інакше після
# вибору першої категорії стан чиститься і повторний пошук іншої
# категорії для того ж місця стає неможливим.
_pending_location: dict[int, tuple[float, float]] = {}


class Nearby(StatesGroup):
    waiting_address = State()


def _ikb_categories() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"nb:{key}")] for key, (label, _, _) in NEARBY_CATEGORIES.items()]
    rows.append([InlineKeyboardButton(text="🏠 Завершити", callback_data="nb_exit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🗺 Що поруч")
async def nearby_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(Nearby.waiting_address)
    await msg.answer(
        "🗺 *Що поруч?*\n\nНапиши адресу або місце (напр. `Київ, Хрещатик 10`), "
        "або надішли геолокацію.",
        reply_markup=kb_cancel(),
    )


async def _geocode(query: str) -> tuple[float, float] | None:
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "todo-list-telegram-bot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(NOMINATIM_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    logger.warning("Nominatim status=%s for query=%s", resp.status, query)
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
    _pending_location[msg.from_user.id] = (msg.location.latitude, msg.location.longitude)
    await state.clear()
    # Прибираємо стару reply-клавіатуру з "❌ Скасувати" — вона більше
    # ні на що не реагує, бо стан вже очищений, і далі працюємо тільки
    # через inline-кнопки категорій.
    await msg.answer("✅ Локацію отримано.", reply_markup=ReplyKeyboardRemove())
    await msg.answer("Що шукаємо поруч?", reply_markup=_ikb_categories())


@router.message(Nearby.waiting_address)
async def nearby_address(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    coords = await _geocode(msg.text.strip())
    if not coords:
        return await msg.answer(
            "🤔 Не знайшов таку адресу. Спробуй точніше (напр. з містом і країною) "
            "або надішли геолокацію."
        )

    _pending_location[msg.from_user.id] = coords
    await state.clear()
    # Те саме — прибираємо стару reply-клавіатуру "❌ Скасувати", вона
    # вже нерелевантна на цьому кроці.
    await msg.answer("✅ Місце знайдено.", reply_markup=ReplyKeyboardRemove())
    await msg.answer("Що шукаємо поруч?", reply_markup=_ikb_categories())


async def _query_overpass(query: str) -> list[dict] | None:
    for url in OVERPASS_URLS:
        try:
            async with aiohttp.ClientSession(headers=OVERPASS_HEADERS) as session:
                async with session.post(url, data={"data": query}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Overpass %s returned status=%s body=%s", url, resp.status, body[:500])
                        continue
                    data = await resp.json()
                    return data.get("elements", [])
        except asyncio.TimeoutError:
            logger.warning("Overpass %s timed out", url)
            continue
        except Exception:
            logger.exception("Overpass %s failed", url)
            continue
    return None


@router.callback_query(F.data.startswith("nb:"))
async def nearby_category(cb: CallbackQuery):
    key = cb.data.split(":", 1)[1]
    entry = NEARBY_CATEGORIES.get(key)
    if not entry:
        return await cb.answer()
    label, tag_key, tag_val = entry

    coords = _pending_location.get(cb.from_user.id)
    if not coords:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Локація втрачена, почни спочатку через «🗺 Що поруч».")

    lat, lon = coords
    await cb.answer("Шукаю...")

    query = f"""
[out:json][timeout:18];
node["{tag_key}"="{tag_val}"](around:1500,{lat},{lon});
out body 15;
"""
    elements = await _query_overpass(query)

    if elements is None:
        return await cb.message.edit_text(
            f"{label}\n\n⚠️ Сервіс пошуку тимчасово перевантажений. Спробуй ще раз за хвилину.",
            reply_markup=_ikb_categories(),
        )

    if not elements:
        text = f"{label}\n\n📭 Нічого не знайдено поруч (1.5 км)."
    else:
        lines = [f"{label} поруч:\n"]
        for el in elements[:10]:
            name = el.get("tags", {}).get("name", "Без назви")
            lines.append(f"• {name}")
        text = "\n".join(lines)

    await cb.message.edit_text(text)
    await cb.message.answer("Шукати ще щось поруч?", reply_markup=_ikb_categories())


@router.callback_query(F.data == "nb_exit")
async def nearby_exit(cb: CallbackQuery):
    _pending_location.pop(cb.from_user.id, None)
    await cb.answer()
    try:
        await cb.message.edit_text("🗺 Пошук завершено.")
    except Exception:
        pass
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())