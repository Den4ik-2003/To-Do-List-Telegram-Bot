import re
from datetime import date

from database import events as events_db

MONTHS_GENITIVE = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MONTHS_GENITIVE.keys()) + r")(?:\s+(\d{4}))?",
    re.IGNORECASE,
)
NUMERIC_DATE_PATTERN = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b")
MONTHLY_PATTERN = re.compile(r"щомісяця\s+(\d{1,2})", re.IGNORECASE)

SPECIAL_DATES = {
    "нового року": (1, 1),
    "нового рока": (1, 1),
    "різдва": (25, 12),
}


def _next_occurrence(day: int, month: int, year: int | None) -> date | None:
    today = date.today()
    try:
        if year:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def _parse_explicit_date(text: str) -> date | None:
    for phrase, (day, month) in SPECIAL_DATES.items():
        if phrase in text:
            return _next_occurrence(day, month, None)

    m = DATE_PATTERN.search(text)
    if m:
        day = int(m.group(1))
        month = MONTHS_GENITIVE[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else None
        return _next_occurrence(day, month, year)

    m2 = NUMERIC_DATE_PATTERN.search(text)
    if m2:
        day, month = int(m2.group(1)), int(m2.group(2))
        year = int(m2.group(3)) if m2.group(3) else None
        return _next_occurrence(day, month, year)

    return None


def parse_monthly(text: str) -> int | None:
    m = MONTHLY_PATTERN.search(text)
    if not m:
        return None
    day = int(m.group(1))
    return day if 1 <= day <= 31 else None


def parse_date_for_event(text: str) -> tuple[int, int, int | None] | None:
    """Повертає (day, month, year|None) для збереження події (без обчислення 'наступної')."""
    m = DATE_PATTERN.search(text)
    if m:
        day = int(m.group(1))
        month = MONTHS_GENITIVE[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else None
        return day, month, year
    m2 = NUMERIC_DATE_PATTERN.search(text)
    if m2:
        day, month = int(m2.group(1)), int(m2.group(2))
        year = int(m2.group(3)) if m2.group(3) else None
        return day, month, year
    return None


def _format_countdown(target: date, label: str) -> str:
    days = (target - date.today()).days
    if days == 0:
        return f"📅 Сьогодні {label}! 🎉"
    if days == 1:
        return f"📅 Завтра {label}!"
    return f"📅 До «{label}» залишилось: *{days} дн.* ({target.strftime('%d.%m.%Y')})"


async def try_answer(uid: int, text: str) -> str | None:
    """
    Пробує відповісти на 'скільки днів до ...' без AI.
    Повертає готову відповідь або None (тоді текст іде далі, в AI-чат).
    """
    lowered = text.lower().strip()
    if "до" not in lowered and "скільки" not in lowered:
        return None

    explicit = _parse_explicit_date(lowered)
    if explicit:
        label = lowered.split("до", 1)[-1].strip(" ?!.") or "цієї дати"
        return _format_countdown(explicit, label)

    events = await events_db.get_user_events(uid)
    for ev in events:
        name = (ev.get("name") or "").lower()
        if name and name in lowered:
            target = events_db.next_event_date(ev)
            if target:
                return _format_countdown(target, ev["name"])

    return None