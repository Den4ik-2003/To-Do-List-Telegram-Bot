from datetime import datetime


def is_valid_date_str(raw: str) -> bool:
    try:
        datetime.strptime(raw.strip(), "%d.%m.%Y")
        return True
    except (ValueError, TypeError):
        return False


def is_valid_time_str(raw: str) -> bool:
    try:
        datetime.strptime(raw.strip(), "%H:%M")
        return True
    except (ValueError, TypeError):
        return False


def is_valid_datetime_str(raw: str) -> bool:
    try:
        datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
        return True
    except (ValueError, TypeError):
        return False


def parse_amount(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = (
        raw.strip()
        .replace("грн", "")
        .replace("UAH", "")
        .replace("₴", "")
        .replace(" ", "")
        .replace(",", ".")
    )
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return round(amount, 2)


def sanitize_text(raw: str, max_len: int = 200) -> str:
    return (raw or "").strip()[:max_len]


def is_empty_or_dash(raw: str) -> bool:
    return not raw or raw.strip() == "-"


def is_valid_time_range(hour: int, minute: int) -> bool:
    return 0 <= hour <= 23 and 0 <= minute <= 59