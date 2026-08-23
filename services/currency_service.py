import logging
import re

import aiohttp

from database import rates as rates_db

logger = logging.getLogger("tasks_bot")

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={code}&json"
SUPPORTED = ["USD", "EUR", "PLN", "CNY"]

CURRENCY_ALIASES = {
    "usd": "USD", "$": "USD", "долар": "USD", "долари": "USD", "доларів": "USD",
    "eur": "EUR", "€": "EUR", "євро": "EUR",
    "pln": "PLN", "zl": "PLN", "zł": "PLN", "злотих": "PLN", "злотий": "PLN",
    "uah": "UAH", "грн": "UAH", "₴": "UAH", "гривень": "UAH", "гривні": "UAH",
    "cny": "CNY", "rmb": "CNY", "юань": "CNY", "юані": "CNY", "юанів": "CNY", "元": "CNY",
}

# "1500 PLN → UAH", "200$ в PLN", "50 eur to usd"
CONVERT_PATTERN = re.compile(
    r"(\d+[.,]?\d*)\s*([a-zа-яё$€₴元]+)\s*(?:->|→|в|to|у)\s*([a-zа-яё$€₴元]+)",
    re.IGNORECASE,
)


async def fetch_nbu_rate(code: str) -> float | None:
    url = NBU_URL.format(code=code)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("NBU API повернув статус %s для %s", resp.status, code)
                    return None
                data = await resp.json(content_type=None)
                if not data:
                    return None
                return float(data[0]["rate"])
    except Exception:
        logger.exception("Не вдалося отримати курс %s з НБУ", code)
        return None


async def update_rates() -> dict | None:
    """Тягне свіжі курси USD/EUR/PLN/CNY з НБУ і зберігає в Mongo."""
    rates = {}
    for code in SUPPORTED:
        rate = await fetch_nbu_rate(code)
        if rate is not None:
            rates[code] = rate
        else:
            logger.warning("Курс %s не оновлено", code)
    if not rates:
        return None
    await rates_db.save_rates(rates)
    logger.info("Курси валют оновлено: %s", rates)
    return await rates_db.get_rates()


async def get_current_rates() -> dict | None:
    doc = await rates_db.get_rates()
    if not doc:
        doc = await update_rates()
    return doc


def _normalize_currency(raw: str) -> str | None:
    return CURRENCY_ALIASES.get(raw.strip().lower())


async def try_convert(text: str) -> str | None:
    """
    Пробує розпарсити текст на кшталт '1500 PLN → UAH'.
    Повертає готову відповідь, або None якщо текст не схожий на запит конвертації
    (тоді виклик передається далі — у звичайний AI-чат чи ігнорується).
    """
    match = CONVERT_PATTERN.search(text)
    if not match:
        return None

    amount_str, from_raw, to_raw = match.groups()
    try:
        amount = float(amount_str.replace(",", "."))
    except ValueError:
        return None

    from_cur = _normalize_currency(from_raw)
    to_cur = _normalize_currency(to_raw)
    if not from_cur or not to_cur:
        return None
    if from_cur == to_cur:
        return f"💰 {amount:g} {from_cur} = {amount:g} {to_cur}"

    rates = await get_current_rates()
    if not rates:
        return "⚠️ Не вдалося отримати курси валют, спробуй пізніше."

    def to_uah(cur: str, val: float) -> float | None:
        if cur == "UAH":
            return val
        rate = rates.get(cur)
        return val * rate if rate else None

    uah_amount = to_uah(from_cur, amount)
    if uah_amount is None:
        return f"⚠️ Немає курсу для {from_cur}."

    if to_cur == "UAH":
        result = uah_amount
    else:
        rate = rates.get(to_cur)
        if not rate:
            return f"⚠️ Немає курсу для {to_cur}."
        result = uah_amount / rate

    return f"💰 {amount:g} {from_cur} = {result:.2f} {to_cur}"


async def convert_to_uah(amount: float, currency: str) -> float | None:
    currency = currency.upper()
    if currency == "UAH":
        return amount
    rates = await get_current_rates()
    if not rates:
        return None
    rate = rates.get(currency)
    if not rate:
        return None
    return amount * rate