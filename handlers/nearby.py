import asyncio
import logging
import time

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
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
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

# Кандидати адреси (коли пошук неоднозначний, напр. просто назва вулиці
# без міста), поки користувач не обере потрібний варіант.
_pending_geocode_options: dict[int, list[dict]] = {}

# Короткочасний кеш результатів Overpass: (округлені lat/lon, категорія) -> (timestamp, elements).
# Overpass-дзеркала часто лежать одночасно всі разом — якщо хтось щойно
# успішно шукав ту саму категорію в тій самій точці, віддаємо готовий
# результат миттєво замість нового звернення, яке може знову впасти.
_RESULT_CACHE_TTL = 15 * 60  # 15 хвилин
_result_cache: dict[tuple, tuple[float, list]] = {}


def _cache_key(lat: float, lon: float, category: str) -> tuple:
    # Округлюємо координати до ~100м, щоб невеликий дрейф GPS/геокодера
    # не створював окремий кеш-запис на кожен мікроскопічний зсув.
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
    """
    Повертає до `limit` кандидатів для неоднозначних запитів (напр. просто
    назва вулиці без міста). Кожен елемент: {"lat", "lon", "display_name", "short"}.
    """
    params = {"q": query, "format": "json", "limit": limit, "addressdetails": 1}
    headers = {"User-Agent": "todo-list-telegram-bot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(NOMINATIM_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    logger.warning("Nominatim status=%s for query=%s", resp.status, query)
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
        # Прибираємо дублікати, коли Nominatim повертає однакове місце
        # кілька разів під різними тегами (напр. вулиця і зупинка на ній).
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

    # Кілька варіантів (напр. однойменна вулиця в різних містах) —
    # показуємо на вибір, щоб не брати навмання перший результат.
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


async def _apply_location(uid: int, opt: dict, target: Message, state: FSMContext, edit: bool = False):
    _pending_location[uid] = (opt["lat"], opt["lon"])
    await state.clear()

    text = f"✅ Місце знайдено: {opt['short']}"
    if edit:
        await target.edit_text(text)
    else:
        # Прибираємо стару reply-клавіатуру "❌ Скасувати" — вона вже
        # нерелевантна на цьому кроці.
        await target.answer(text, reply_markup=ReplyKeyboardRemove())

    await target.answer("Що шукаємо поруч?", reply_markup=_ikb_categories())


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

    cached = _cache_get(lat, lon, key)
    if cached is not None:
        await cb.answer()
        await cb.message.edit_text(_fmt_results(label, cached))
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
    await cb.message.edit_text(_fmt_results(label, elements))
    await cb.message.answer("Шукати ще щось поруч?", reply_markup=_ikb_categories())


def _fmt_results(label: str, elements: list) -> str:
    if not elements:
        return f"{label}\n\n📭 Нічого не знайдено поруч (1.5 км)."
    lines = [f"{label} поруч:\n"]
    for el in elements[:10]:
        name = el.get("tags", {}).get("name", "Без назви")
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