import logging
import re

import aiohttp

logger = logging.getLogger("tasks_bot")

MARKS_URL = "http://api.auto.ria.com/categories/1/marks"
MODELS_URL = "http://api.auto.ria.com/categories/1/marks/{marka_id}/models/_group"
SEARCH_BASE_URL = "https://auto.ria.com/uk/search/"

FUEL_IDS = {
    "бензин": 1,
    "дизель": 2,
    "газ/бензин": 3,
    "гібрид": 4,
    "електро": 5,
}
GEARBOX_IDS = {
    "механіка": 1,
    "автомат": 2,
    "робот": 4,
    "варіатор": 3,
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)

_marks_cache: list[dict] | None = None
_models_cache: dict[int, list[dict]] = {}


async def _fetch_marks() -> list[dict]:
    global _marks_cache
    if _marks_cache is not None:
        return _marks_cache
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(MARKS_URL) as resp:
                if resp.status != 200:
                    logger.warning("AUTO.RIA marks status=%s", resp.status)
                    return []
                data = await resp.json()
                _marks_cache = data
                return data
    except Exception:
        logger.exception("AUTO.RIA marks fetch failed")
        return []


async def find_brand_id(name: str) -> tuple[int, str] | None:
    """Шукає марку за назвою (нечутливо до регістру, часткове співпадіння).
    Повертає (marka_id, справжня_назва) або None."""
    marks = await _fetch_marks()
    name_lower = name.strip().lower()
    for m in marks:
        if m.get("name", "").lower() == name_lower:
            return m["value"], m["name"]
    for m in marks:
        if name_lower in m.get("name", "").lower():
            return m["value"], m["name"]
    return None


def _flatten_models_response(raw) -> list[dict]:
    """API повертає моделі або як список ГРУП (list[list[dict]]), або вже
    ПЛАСКИЙ список (list[dict]) — обробляємо обидва варіанти безпечно."""
    flat: list[dict] = []
    if not isinstance(raw, list):
        return flat

    for entry in raw:
        if isinstance(entry, dict):
            flat.append(entry)
        elif isinstance(entry, list):
            for item in entry:
                if isinstance(item, dict):
                    flat.append(item)
    return flat


async def _fetch_models(marka_id: int) -> list[dict]:
    if marka_id in _models_cache:
        return _models_cache[marka_id]
    url = MODELS_URL.format(marka_id=marka_id)
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("AUTO.RIA models status=%s for marka_id=%s", resp.status, marka_id)
                    return []
                raw = await resp.json()
                flat = _flatten_models_response(raw)
                _models_cache[marka_id] = flat
                return flat
    except Exception:
        logger.exception("AUTO.RIA models fetch failed for marka_id=%s", marka_id)
        return []


async def find_model_id(marka_id: int, name: str) -> tuple[int, str] | None:
    models = await _fetch_models(marka_id)
    name_lower = name.strip().lower()
    for m in models:
        if m.get("name", "").lower() == name_lower:
            return m["value"], m["name"]
    for m in models:
        if name_lower in m.get("name", "").lower():
            return m["value"], m["name"]
    return None


async def list_top_models(marka_id: int, limit: int = 8) -> list[dict]:
    models = await _fetch_models(marka_id)
    return models[:limit]


def build_search_url(filters: dict) -> str:
    """Будує URL пошуку AUTO.RIA за схемою, підтвердженою двома незалежними
    джерелами (реальний робочий приклад стороннього скрапера + офіційний
    конвертер параметрів RIA): categories.main.id / brand.id[0] /
    model.id[0] / year[0].gte-lte / price.currency + price.USD.gte-lte /
    fuel.id[n] / gearbox.id[n]. Параметри пробігу та міста лишаються
    best-effort — для них немає такого ж підтвердженого джерела, тож варто
    вручну звірити згенероване посилання, якщо вони важливі."""
    params = ["categories.main.id=1"]

    brand_id = filters.get("brand_id")
    model_id = filters.get("model_id")
    if brand_id:
        params.append(f"brand.id[0]={brand_id}")
        params.append(f"model.id[0]={model_id if model_id else 0}")

    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if year_from:
        params.append(f"year[0].gte={year_from}")
    if year_to:
        params.append(f"year[0].lte={year_to}")

    price_from = filters.get("price_from")
    price_to = filters.get("price_to")
    if price_from or price_to:
        # currency=1 означає USD — обов'язковий параметр, без нього
        # price.USD.gte/lte ігнорується сайтом.
        params.append("price.currency=1")
        if price_from:
            params.append(f"price.USD.gte={int(price_from)}")
        if price_to:
            params.append(f"price.USD.lte={int(price_to)}")

    fuel_id = filters.get("fuel_id")
    if fuel_id:
        params.append(f"fuel.id[0]={fuel_id}")

    gearbox_id = filters.get("gearbox_id")
    if gearbox_id:
        params.append(f"gearbox.id[0]={gearbox_id}")

    mileage_from = filters.get("mileage_from")
    mileage_to = filters.get("mileage_to")
    if mileage_from:
        params.append(f"raceInt[0].gte={int(mileage_from)}")
    if mileage_to:
        params.append(f"raceInt[0].lte={int(mileage_to)}")

    city = filters.get("city")
    if city:
        city_clean = re.sub(r"[^\w\sа-яіїєґ-]", "", city, flags=re.IGNORECASE).strip()
        if city_clean:
            params.append(f"city.name={aiohttp.helpers.quote(city_clean)}")

    return SEARCH_BASE_URL + "?" + "&".join(params)


def parse_price_text(raw: str) -> float | None:
    """Парсить ціну з тексту типу '25000', '25k', '$25 000', '500000 грн'."""
    raw = raw.strip().lower().replace(" ", "").replace(",", "")
    if not raw:
        return None
    is_uah = "грн" in raw or "uah" in raw
    raw = re.sub(r"[^\d.k]", "", raw)
    if not raw:
        return None
    multiplier = 1
    if raw.endswith("k"):
        multiplier = 1000
        raw = raw[:-1]
    try:
        value = float(raw) * multiplier
    except ValueError:
        return None
    if is_uah:
        return None
    return value