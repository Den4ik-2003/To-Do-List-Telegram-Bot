import asyncio
import logging
import time
from datetime import datetime, timedelta, date

import aiohttp

logger = logging.getLogger("tasks_bot")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
FETCH_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5  # секунди, зростає з кожною спробою

# Кеш останньої вдалої "сирої" відповіді Open-Meteo по (lat, lon, forecast_days).
# TTL підняли до 3 годин — погода не змінюється щохвилини, а безкоштовний
# ліміт Open-Meteo (10 000 запитів/добу на IP) вичерпується дуже швидко,
# якщо кожне натискання кнопки й кожна ранкова розсилка б'ють в API напряму.
_weather_cache: dict[tuple, dict] = {}
CACHE_TTL_SECONDS = 3 * 60 * 60  # 3 години

# Якщо API повернув 429 (денний ліміт вичерпано) — ретраї марні, це не
# тимчасовий збій. Тут просто фіксуємо факт до півночі UTC (коли ліміт
# скидається), щоб не бити марно в API повторно і одразу йти у fallback.
_rate_limited_until: float = 0.0


def _is_rate_limited() -> bool:
    return time.time() < _rate_limited_until


def _mark_rate_limited():
    global _rate_limited_until
    # Ліміт Open-Meteo скидається опівночі UTC. Ставимо паузу на годину
    # наперед і потім перевіряємо знову — простіше і надійніше, ніж рахувати
    # точний момент півночі UTC.
    _rate_limited_until = time.time() + 60 * 60

WEATHER_CODE_DESCRIPTIONS = {
    0: "☀️ Ясно", 1: "🌤️ Переважно ясно", 2: "⛅ Мінлива хмарність", 3: "☁️ Хмарно",
    45: "🌫 Туман", 48: "🌫 Туман з інеєм",
    51: "🌦 Легка мряка", 53: "🌦 Мряка", 55: "🌧 Сильна мряка",
    61: "🌧 Невеликий дощ", 63: "🌧 Дощ", 65: "🌧 Сильний дощ",
    71: "🌨 Невеликий сніг", 73: "🌨 Сніг", 75: "❄️ Сильний сніг",
    80: "🌦 Зливи", 81: "🌧 Сильні зливи", 82: "⛈ Дуже сильні зливи",
    95: "⛈ Гроза",
}


def _country_matches(result: dict, country_query: str) -> bool:
    country_query = country_query.strip().lower()
    if not country_query:
        return True
    country_name = (result.get("country") or "").lower()
    country_code = (result.get("country_code") or "").lower()
    return country_query in country_name or country_query == country_code


async def search_city_options(query: str, count: int = 6) -> list[dict]:
    if "," in query:
        city_part, country_part = query.split(",", 1)
    else:
        city_part, country_part = query, ""

    city_part = city_part.strip()
    country_part = country_part.strip()

    params = {"name": city_part, "count": max(count, 10), "language": "uk", "format": "json"}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(GEOCODE_URL, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Geocoding status=%s body=%s", resp.status, body[:200])
                    return []
                data = await resp.json()
                results = data.get("results") or []
    except asyncio.TimeoutError:
        logger.warning("Geocoding timeout для запиту '%s'", query)
        return []
    except aiohttp.ClientError as e:
        logger.warning("Geocoding мережева помилка для '%s': %s", query, e)
        return []
    except Exception:
        logger.exception("Не вдалося геокодувати місто %s", query)
        return []

    if country_part:
        filtered = [r for r in results if _country_matches(r, country_part)]
        if filtered:
            results = filtered

    options = []
    for r in results[:count]:
        options.append({
            "name": r.get("name", city_part),
            "country": r.get("country", ""),
            "admin1": r.get("admin1", ""),
            "lat": r["latitude"],
            "lon": r["longitude"],
        })
    return options


def format_option_label(opt: dict) -> str:
    parts = [opt["name"]]
    if opt.get("admin1") and opt["admin1"] != opt["name"]:
        parts.append(opt["admin1"])
    if opt.get("country"):
        parts.append(opt["country"])
    return ", ".join(parts)


def _cache_key(lat: float, lon: float, forecast_days: int) -> tuple:
    # округлюємо координати, щоб дрібні відмінності (0.0001) не плодили окремі
    # записи кешу для фактично того самого міста
    return (round(lat, 3), round(lon, 3), forecast_days)


def _get_cached(key: tuple) -> dict | None:
    cached = _weather_cache.get(key)
    if cached and (time.time() - cached["ts"]) <= CACHE_TTL_SECONDS:
        return cached["data"]
    return None


async def get_weather(lat: float, lon: float, forecast_days: int = 2, attempts: int = FETCH_ATTEMPTS) -> dict | None:
    # ВАЖЛИВО: сучасне Open-Meteo API очікує "weather_code" (з підкресленням).
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "forecast_days": max(1, min(forecast_days, 16)),
        "timezone": "auto",
    }
    key = _cache_key(lat, lon, forecast_days)

    # Денний ліміт API вже вичерпано (зафіксовано нещодавнім 429) — не б'ємо
    # знову марно, одразу віддаємо кеш, якщо він ще не застарів.
    if _is_rate_limited():
        cached = _get_cached(key)
        if cached:
            logger.info("get_weather: денний ліміт Open-Meteo вичерпано, віддаю кеш для (%s, %s)", lat, lon)
        else:
            logger.warning("get_weather: денний ліміт Open-Meteo вичерпано і кешу немає для (%s, %s)", lat, lon)
        return cached

    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(FORECAST_URL, params=params) as resp:
                    if resp.status == 429:
                        body = await resp.text()
                        logger.warning(
                            "Open-Meteo денний ліміт вичерпано (429): %s (lat=%s lon=%s)",
                            body[:300], lat, lon,
                        )
                        _mark_rate_limited()
                        # Ретраї при 429 марні — ліміт не скинеться за секунди
                        break
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "Open-Meteo forecast status=%s body=%s (спроба %s/%s, lat=%s lon=%s)",
                            resp.status, body[:300], attempt, attempts, lat, lon,
                        )
                        last_error = f"status {resp.status}"
                    else:
                        data = await resp.json()
                        _weather_cache[key] = {"data": data, "ts": time.time()}
                        return data
        except asyncio.TimeoutError:
            logger.warning("Open-Meteo forecast timeout (спроба %s/%s) для (%s, %s)", attempt, attempts, lat, lon)
            last_error = "timeout"
        except aiohttp.ClientError as e:
            logger.warning("Open-Meteo forecast мережева помилка (спроба %s/%s): %s", attempt, attempts, e)
            last_error = str(e)
        except Exception:
            logger.exception("Open-Meteo forecast неочікувана помилка (спроба %s/%s) для (%s, %s)", attempt, attempts, lat, lon)
            last_error = "exception"

        if attempt < attempts:
            await asyncio.sleep(RETRY_BASE_DELAY * attempt)

    logger.error("get_weather остаточно провалився (lat=%s lon=%s): %s", lat, lon, last_error)

    # Fallback: якщо свіжий запит не вдався (з будь-якої причини, включно з 429) —
    # віддаємо кеш, якщо він ще не застарів, замість помилки користувачу.
    cached = _get_cached(key)
    if cached:
        logger.info("get_weather: віддаю кешовані дані для (%s, %s)", lat, lon)
        return cached

    return None


def clothing_advice(temp: float, precipitation: float) -> str:
    if temp < -10:
        base = "🧥 Дуже тепла зимова куртка, шапка, шарф, теплі рукавиці"
    elif temp < 0:
        base = "🧥 Тепла куртка, шапка, шарф"
    elif temp < 10:
        base = "🧥 Куртка або пальто"
    elif temp < 18:
        base = "🧶 Светр або легка куртка"
    elif temp < 25:
        base = "👕 Футболка, легка кофта про запас"
    else:
        base = "👕 Легкий одяг, головний убір від сонця"

    if precipitation and precipitation > 0:
        base += "\n☔ Візьми парасольку — очікуються опади"
    return base


def weather_code_description(code: int) -> str:
    return WEATHER_CODE_DESCRIPTIONS.get(code, "🌡 Погода")


def _extract_hourly_window(hourly: dict, local_now: datetime, hours_ahead: int = 15) -> list[dict]:
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precs = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])

    now_floor = local_now.replace(minute=0, second=0, microsecond=0)
    limit = local_now + timedelta(hours=hours_ahead)
    window = []
    for i, t in enumerate(times):
        try:
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        if dt < now_floor or dt > limit:
            continue
        window.append({
            "dt": dt,
            "temp": temps[i] if i < len(temps) else None,
            "precip": precs[i] if i < len(precs) else 0,
            "code": codes[i] if i < len(codes) else 0,
        })
    return window


def _find_rain_periods(window: list[dict], threshold: int = 50) -> list[tuple[str, str]]:
    periods = []
    start = None
    prev_dt = None
    for entry in window:
        is_rain = (entry["precip"] or 0) >= threshold
        if is_rain and start is None:
            start = entry["dt"]
        if not is_rain and start is not None:
            periods.append((start.strftime("%H:%M"), prev_dt.strftime("%H:%M")))
            start = None
        prev_dt = entry["dt"]
    if start is not None and prev_dt is not None:
        periods.append((start.strftime("%H:%M"), prev_dt.strftime("%H:%M")))
    return periods


def _build_day_plan_lines(window: list[dict]) -> list[str]:
    if not window:
        return []

    lines = ["", "📆 *План на найближчі години:*"]
    rain_periods = _find_rain_periods(window)

    if not rain_periods:
        lines.append("✅ Опадів не очікується — гарний час для справ на вулиці.")
    else:
        for start, end in rain_periods:
            lines.append(f"🌧 Дощ орієнтовно {start}–{end}")
        first_start = rain_periods[0][0]
        last_end = rain_periods[-1][1]
        lines.append(f"💡 Раджу зробити справи на вулиці до {first_start} або після {last_end}.")

    temps = [e["temp"] for e in window if e["temp"] is not None]
    if temps:
        lines.append(f"🌡 Діапазон найближчих годин: {min(temps):.0f}°C…{max(temps):.0f}°C")

    return lines


async def build_weather_report(lat: float, lon: float, display_name: str) -> str | None:
    data = await get_weather(lat, lon, forecast_days=2)
    if not data:
        return None

    current = data.get("current")
    if not current:
        logger.error("Open-Meteo відповів без блоку 'current' для (%s, %s): %s", lat, lon, str(data)[:300])
        return None

    temp = current.get("temperature_2m")
    if temp is None:
        logger.error("Open-Meteo відповів без temperature_2m для (%s, %s): %s", lat, lon, current)
        return None

    precipitation = current.get("precipitation", 0)
    code = current.get("weather_code", 0)
    wind = current.get("wind_speed_10m")

    lines = [
        f"🌤️ *Погода — {display_name}*",
        "",
        weather_code_description(code),
        f"🌡 Температура: *{temp:.0f}°C*",
    ]
    if wind is not None:
        lines.append(f"💨 Вітер: {wind:.0f} км/год")
    lines.append("")
    lines.append(f"👕 *Що вдягнути:*\n{clothing_advice(temp, precipitation)}")

    hourly = data.get("hourly")
    if hourly:
        current_time_str = current.get("time")
        try:
            local_now = datetime.strptime(current_time_str, "%Y-%m-%dT%H:%M") if current_time_str else datetime.now()
        except ValueError:
            local_now = datetime.now()
        window = _extract_hourly_window(hourly, local_now)
        lines.extend(_build_day_plan_lines(window))

    return "\n".join(lines)


async def build_hourly_day_report(lat: float, lon: float, display_name: str, target_date: date) -> str | None:
    """Погодинний прогноз (00:00–23:00) на конкретну дату."""
    today = date.today()
    days_ahead = (target_date - today).days
    if days_ahead < 0:
        return None  # погода на минулу дату недоступна через forecast API
    forecast_days = max(1, min(days_ahead + 1, 16))

    data = await get_weather(lat, lon, forecast_days=forecast_days)
    if not data:
        return None

    hourly = data.get("hourly")
    if not hourly:
        return None

    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precs = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])

    date_prefix = target_date.isoformat()  # "YYYY-MM-DD"
    lines = [f"📅 *Погодинно на {target_date.strftime('%d.%m.%Y')} — {display_name}*", ""]
    found = False

    for i, t in enumerate(times):
        if not t.startswith(date_prefix):
            continue
        found = True
        hour_label = t.split("T", 1)[1]
        temp = temps[i] if i < len(temps) else None
        precip = precs[i] if i < len(precs) else 0
        code = codes[i] if i < len(codes) else 0

        icon = weather_code_description(code).split(" ", 1)[0]
        temp_str = f"{temp:.0f}°C" if temp is not None else "?"
        line = f"{hour_label}  {icon}  {temp_str}"
        if precip and precip >= 40:
            line += f"  💧{precip:.0f}%"
        lines.append(line)

    if not found:
        return None

    return "\n".join(lines)