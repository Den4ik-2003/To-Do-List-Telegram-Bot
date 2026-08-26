import asyncio
import logging
import time

import aiohttp
from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="nearby")

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

OVERPASS_HEADERS = {
    "User-Agent": "todo-list-telegram-bot/1.0 (contact: telegram bot)",
    "Accept": "application/json",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
}
NOMINATIM_HEADERS = {"User-Agent": "todo-list-telegram-bot/1.0 (contact: telegram bot)"}

NEARBY_CATEGORIES = {
    "pharmacy": ("💊 Аптека", "amenity", "pharmacy"),
    "shop": ("🛒 Магазин", "shop", "supermarket"),
    "fuel": ("⛽ Заправка", "amenity", "fuel"),
    "atm": ("🏧 Банкомат", "amenity", "atm"),
}

_pending_location: dict[int, tuple[float, float]] = {}
_pending_geocode_options: dict[int, list[dict]] = {}

_RESULT_CACHE_TTL = 15 * 60  # 15 хвилин
_result_cache: dict[tuple, tuple[float, list]] = {}

# Кеш reverse-геокодування (координати -> назва вулиці) — тримаємо довго,
# бо вулиця біля точки практично ніколи не змінюється, а Nominatim дозволяє
# лише 1 запит/сек, тому повторні виклики для тих самих точок дорогі.
_REVERSE_CACHE_TTL = 7 * 24 * 60 * 60  # 7 днів
_reverse_cache: dict[tuple, tuple[float, str | None]] = {}

# Nominatim Usage Policy дозволяє максимум 1 запит/сек — тримаємо один
# спільний лок і час останнього виклику, щоб усі reverse-запити (навіть
# для різних користувачів одночасно) не перевищували цей ліміт.
_nominatim_lock = asyncio.Lock()
_last_nominatim_call = 0.0


def _cache_key(lat: float, lon: float, category: str) -> tuple:
    return (round(lat, 3), round(lon, 3), category)


def _cache_get(lat: float, lon: float, category: str) -> list | None:
    key = _cache_key(lat, lon, category)
    entry = _result_cache.get(key)
    if not entry:
        return None
    ts, elements = entry
    if time.monotonic() - ts > _RESULT_CACHE_TTL:
        _result_cache.pop(key, None)
        return None
    return elements


def _cache_set(lat: float, lon: float, category: str, elements: list) -> None:
    _result_cache[_cache_key(lat, lon, category)] = (time.monotonic(), elements)


class Nearby(StatesGroup):
    waiting_address = State()


def _ikb_categories() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"nb:{key}")] for key, (label, _, _) in NEARBY_CATEGORIES.items()]
    rows.append([InlineKeyboardButton(text="🏠 Завершити", callback_data="nb_exit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ikb_address_options(options: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([InlineKeyboardButton(text=opt["short"], callback_data=f"nb_addr:{i}")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="nb_addr_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _shorten_display_name(display_name: str, limit: int = 60) -> str:
    return display_name if len(display_name) <= limit else display_name[: limit - 1] + "…"


@router.message(F.text == "🗺 Що поруч")
async def nearby_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(Nearby.waiting_address)
    await msg.answer(
        "🗺 *Що поруч?*\n\nНапиши адресу, назву вулиці (напр. `Хрещатик` або "
        "`Київ, Хрещатик 10`), або надішли геолокацію.\n\n"
        "Якщо вулиця зустрічається в кількох містах — покажу варіанти на вибір.",
        reply_markup=kb_cancel(),
    )


async def _geocode_options(query: str, limit: int = 5) -> list[dict]:
    params = {"q": query, "format": "json", "limit": limit, "addressdetails": 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(NOMINATIM_SEARCH_URL, params=params, headers=NOMINATIM_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    logger.warning("Nominatim search status=%s for query=%s", resp.status, query)
                    return []
                data = await resp.json()
    except Exception:
        logger.exception("Geocoding failed for query=%s", query)
        return []

    options = []
    seen = set()
    for item in data:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, ValueError):
            continue
        display_name = item.get("display_name", query)
        dedup_key = (round(lat, 3), round(lon, 3))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        options.append({
            "lat": lat,
            "lon": lon,
            "display_name": display_name,
            "short": _shorten_display_name(display_name),
        })
    return options


async def _reverse_geocode_street(lat: float, lon: float) -> str | None:
    """
    Визначає назву вулиці за координатами через Nominatim reverse-геокодування —
    використовується як fallback для об'єктів з Overpass, у яких немає прямих
    тегів addr:street/addr:housenumber (це поширено для дрібних магазинів в OSM).
    Дотримується ліміту Nominatim в 1 запит/сек через спільний лок.
    """
    global _last_nominatim_call

    key = (round(lat, 4), round(lon, 4))
    cached = _reverse_cache.get(key)
    if cached and (time.monotonic() - cached[0]) <= _REVERSE_CACHE_TTL:
        return cached[1]

    async with _nominatim_lock:
        elapsed = time.monotonic() - _last_nominatim_call
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

        params = {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 1}
        street = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    NOMINATIM_REVERSE_URL, params=params, headers=NOMINATIM_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    _last_nominatim_call = time.monotonic()
                    if resp.status == 200:
                        data = await resp.json()
                        addr = data.get("address", {})
                        road = addr.get("road") or addr.get("pedestrian") or addr.get("footway")
                        house = addr.get("house_number", "")
                        if road:
                            street = f"{road} {house}".strip()
                    else:
                        logger.warning("Nominatim reverse status=%s for (%s, %s)", resp.status, lat, lon)
        except Exception:
            logger.exception("Reverse geocoding failed for (%s, %s)", lat, lon)
            _last_nominatim_call = time.monotonic()

    _reverse_cache[key] = (time.monotonic(), street)
    return street


@router.message(Nearby.waiting_address, F.location)
async def nearby_location(msg: Message, state: FSMContext):
    _pending_location[msg.from_user.id] = (msg.location.latitude, msg.location.longitude)
    await state.clear()
    await msg.answer("✅ Локацію отримано.", reply_markup=ReplyKeyboardRemove())
    await _send_categories_prompt(msg.from_user.id, msg)


@router.message(Nearby.waiting_address)
async def nearby_address(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    query = msg.text.strip()
    options = await _geocode_options(query)

    if not options:
        return await msg.answer(
            "🤔 Не знайшов таку адресу чи вулицю. Спробуй точніше (напр. з містом і країною) "
            "або надішли геолокацію."
        )

    if len(options) == 1:
        await _apply_location(msg.from_user.id, options[0], msg, state)
        return

    _pending_geocode_options[msg.from_user.id] = options
    await state.clear()
    await msg.answer(
        f"🔎 Знайшов кілька варіантів для «{query}»:",
        reply_markup=_ikb_address_options(options),
    )


@router.callback_query(F.data.startswith("nb_addr:"))
async def nearby_address_pick(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    options = _pending_geocode_options.get(uid)
    if not options:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Варіанти застаріли, спробуй пошук ще раз.")

    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(options):
        return await cb.answer()

    chosen = options[idx]
    _pending_geocode_options.pop(uid, None)
    await cb.answer()
    await _apply_location(uid, chosen, cb.message, state, edit=True)


@router.callback_query(F.data == "nb_addr_cancel")
async def nearby_address_cancel(cb: CallbackQuery, state: FSMContext):
    _pending_geocode_options.pop(cb.from_user.id, None)
    await state.clear()
    await cb.answer()
    await cb.message.edit_text("Скасовано.")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())


async def _send_categories_prompt(uid: int, target: Message):
    try:
        await target.answer("Що шукаємо поруч?", reply_markup=_ikb_categories())
    except TelegramAPIError:
        logger.exception("Не вдалося надіслати кнопки категорій для uid=%s", uid)
        try:
            await target.answer(
                "⚠️ Не вдалося показати кнопки. Спробуй натиснути «🗺 Що поруч» ще раз.",
                reply_markup=kb_main(),
            )
        except TelegramAPIError:
            pass


async def _apply_location(uid: int, opt: dict, target: Message, state: FSMContext, edit: bool = False):
    _pending_location[uid] = (opt["lat"], opt["lon"])
    await state.clear()

    text = f"✅ Місце знайдено: {opt['short']}"

    if edit:
        try:
            await target.edit_text(text)
        except TelegramAPIError:
            logger.warning("Не вдалося відредагувати повідомлення локації для uid=%s, надсилаю нове", uid)
            try:
                await target.answer(text)
            except TelegramAPIError:
                logger.exception("Не вдалося надіслати підтвердження локації для uid=%s", uid)
    else:
        try:
            await target.answer(text, reply_markup=ReplyKeyboardRemove())
        except TelegramAPIError:
            logger.exception("Не вдалося надіслати підтвердження локації для uid=%s", uid)

    await _send_categories_prompt(uid, target)


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
        return await cb.message.edit_text(
            "⚠️ Локація втрачена (можливо, бот щойно перезапустився). "
            "Почни спочатку через «🗺 Що поруч»."
        )

    lat, lon = coords

    cached = _cache_get(lat, lon, key)
    if cached is not None:
        await cb.answer()
        await cb.message.edit_text("🔎 Уточнюю адреси...")
        text = await _build_results_text(label, cached)
        await cb.message.edit_text(text)
        await cb.message.answer("Шукати ще щось поруч?", reply_markup=_ikb_categories())
        return

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

    _cache_set(lat, lon, key, elements)

    # Уточнення адрес через reverse-геокодування може зайняти кілька секунд
    # (Nominatim обмежує 1 запит/сек) — показуємо проміжний статус.
    await cb.message.edit_text("🔎 Уточнюю адреси...")
    text = await _build_results_text(label, elements)
    await cb.message.edit_text(text)
    await cb.message.answer("Шукати ще щось поруч?", reply_markup=_ikb_categories())


async def _build_results_text(label: str, elements: list) -> str:
    if not elements:
        return f"{label}\n\n📭 Нічого не знайдено поруч (1.5 км)."

    lines = [f"{label} поруч:\n"]
    for el in elements[:10]:
        tags = el.get("tags", {})
        name = tags.get("name", "Без назви")

        street = tags.get("addr:street", "")
        house = tags.get("addr:housenumber", "")
        address = f"{street} {house}".strip()

        if not address:
            # Fallback: у об'єкта немає прямих тегів адреси — визначаємо
            # найближчу вулицю за координатами через reverse-геокодування.
            lat = el.get("lat")
            lon = el.get("lon")
            if lat is not None and lon is not None:
                address = await _reverse_geocode_street(lat, lon) or ""

        if address:
            lines.append(f"• {name} — {address}")
        else:
            lines.append(f"• {name}")
    return "\n".join(lines)


@router.callback_query(F.data == "nb_exit")
async def nearby_exit(cb: CallbackQuery):
    _pending_location.pop(cb.from_user.id, None)
    _pending_geocode_options.pop(cb.from_user.id, None)
    await cb.answer()
    try:
        await cb.message.edit_text("🗺 Пошук завершено.")
    except Exception:
        pass
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())