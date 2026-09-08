from datetime import datetime, timedelta

from database import worktime as worktime_db

MONTH_NAMES_UA = {
    1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
    5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
    9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
}


def parse_hours(text: str) -> float | None:
    raw = (text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or value > 24:
        return None
    return round(value, 2)


def fmt_hours(h: float) -> str:
    if h == int(h):
        return f"{int(h)}"
    return f"{h}".rstrip("0").rstrip(".")


def fmt_date_display(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day} {MONTH_NAMES_UA[d.month].lower()}"


def build_bar(hours: float, max_hours: float = 12.0, length: int = 10) -> str:
    filled = min(length, round((hours / max_hours) * length)) if max_hours else 0
    filled = max(0, filled)
    return "█" * filled + "░" * (length - filled)


async def get_period_stats(uid: int, start_date: str, end_date: str) -> dict:
    entries = await worktime_db.get_entries_range(uid, start_date, end_date)
    total = sum(e["hours"] for e in entries)
    avg = round(total / len(entries), 1) if entries else 0.0
    return {"entries": entries, "total": round(total, 1), "avg": avg, "count": len(entries)}


async def build_period_report(uid: int, start_date: str, end_date: str, title: str) -> str:
    stats = await get_period_stats(uid, start_date, end_date)
    if not stats["entries"]:
        return f"📊 *{title}*\n\n📭 Немає записів за цей період."
    lines = [f"📊 *{title}*", ""]
    for e in stats["entries"]:
        bar = build_bar(e["hours"])
        lines.append(f"{fmt_date_display(e['date'])} — {fmt_hours(e['hours'])} год  {bar}")
    lines.append("")
    lines.append(f"Разом: {fmt_hours(stats['total'])} год")
    lines.append(f"Середнє: {stats['avg']} год/день")
    return "\n".join(lines)


async def build_today_report(uid: int) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    entry = await worktime_db.get_entry(uid, today)
    if not entry:
        return "📊 *Сьогодні*\n\n📭 Ще немає запису за сьогодні."
    return f"📊 *Сьогодні*\n\n{fmt_date_display(today)} — {fmt_hours(entry['hours'])} год"


async def build_week_report(uid: int) -> str:
    end = datetime.now()
    start = end - timedelta(days=6)
    return await build_period_report(uid, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "Останні 7 днів")


async def build_month_report(uid: int) -> str:
    now = datetime.now()
    start = now.replace(day=1)
    title = MONTH_NAMES_UA[now.month]
    return await build_period_report(uid, start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"), title)