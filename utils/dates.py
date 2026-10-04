import calendar
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

from config.constants import STATUS_PENDING

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

DEFAULT_TZ = "Europe/Kyiv"
WEEKDAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


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


def now_local(tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)


def local_to_utc(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def schedule_matches(s: dict, d: date, start: date) -> bool:
    t = s.get("type")
    interval = max(int(s.get("interval") or 1), 1)
    days = s.get("days") or []
    if t == "daily":
        return True
    if t == "every_n_days":
        return (d - start).days % interval == 0
    if t == "weekdays":
        return d.weekday() < 5
    if t == "weekends":
        return d.weekday() >= 5
    if t == "days":
        return d.weekday() in days
    if t == "weekly":
        wd = days or [start.weekday()]
        if d.weekday() not in wd:
            return False
        weeks = ((d - timedelta(days=d.weekday())) - (start - timedelta(days=start.weekday()))).days // 7
        return weeks % interval == 0
    if t == "monthly":
        day = int(s.get("day") or start.day)
        return d.day == min(day, calendar.monthrange(d.year, d.month)[1])
    return False


def next_occurrence(s: dict, from_date: date, start: date, end: date | None = None) -> date | None:
    d = max(from_date, start)
    for _ in range(800):
        if end and d > end:
            return None
        if schedule_matches(s, d, start):
            return d
        d += timedelta(days=1)
    return None


def build_schedule(kind: str, hhmm: str, tz: str, days=None, interval: int = 1, day: int | None = None) -> dict:
    s = {"type": kind, "time": hhmm, "days": [], "interval": 1, "day": None, "tz": tz}
    if kind == "biweekly":
        s.update(type="weekly", interval=2, days=sorted(days or []))
    elif kind == "weekly":
        s.update(type="weekly", days=sorted(days or []))
    elif kind == "days":
        s.update(type="days", days=sorted(days or []))
    elif kind == "every_n":
        s.update(type="every_n_days", interval=max(int(interval), 1))
    elif kind in ("monthly", "monthday"):
        s.update(type="monthly", day=day)
    return s


def describe_schedule(s: dict) -> str:
    t = s.get("type")
    interval = int(s.get("interval") or 1)
    days = " / ".join(WEEKDAYS_UA[i] for i in sorted(s.get("days") or []))
    if t == "daily":
        base = "Щодня"
    elif t == "every_n_days":
        base = f"Кожні {interval} дн."
    elif t == "weekdays":
        base = "По буднях"
    elif t == "weekends":
        base = "По вихідних"
    elif t == "days":
        base = days
    elif t == "weekly":
        prefix = "Щотижня" if interval == 1 else f"Кожні {interval} тижні"
        base = f"{prefix}: {days}" if days else prefix
    elif t == "monthly":
        base = f"Щомісяця, {s['day']}-го числа" if s.get("day") else "Щомісяця"
    else:
        base = "—"
    return f"{base} о {s.get('time', '')}"