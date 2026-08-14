from datetime import datetime, timedelta

from config.constants import STATUS_PENDING

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]


def parse_due(due_str: str) -> datetime | None:
    try:
        return datetime.strptime(due_str, "%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return None


def fmt_due(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def is_today(due_str: str) -> bool:
    dt = parse_due(due_str)
    return bool(dt and dt.date() == datetime.now().date())


def is_missed(t: dict) -> bool:
    if t.get("status") != STATUS_PENDING:
        return False
    due = parse_due(t.get("due", ""))
    return bool(due and due <= datetime.now())


def time_remaining_str(due_dt: datetime) -> str:
    now = datetime.now()
    secs = (due_dt - now).total_seconds()
    if secs <= 0:
        return "⚫ Прострочено"
    if secs <= 1800:
        m = max(1, int(secs // 60))
        return f"🔴 Через {m} хв"
    if due_dt.date() == now.date():
        total_min = int(secs // 60)
        h, m = divmod(total_min, 60)
        parts = []
        if h:
            parts.append(f"{h} год")
        if m:
            parts.append(f"{m} хв")
        return "🟢 Через " + " ".join(parts) if parts else "🟢 Скоро"
    if due_dt.date() == (now + timedelta(days=1)).date():
        return "🟡 Завтра"
    days = (due_dt.date() - now.date()).days
    return f"⚫ Через {days} дн."


def fmt_duration(seconds: float) -> str:
    total_min = int(seconds // 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"


def next_daily_target(hour: int, minute: int) -> datetime:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target