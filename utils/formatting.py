from config.constants import (
    LABELS,
    CATEGORIES,
    PRIORITY_EMOJI,
    STATUS_DONE,
    PROJECT_ACTIVE,
)
from utils.dates import is_missed, fmt_duration, parse_due, time_remaining_str


def render_progress_bar(current: float, total: float, length: int = 12) -> str:
    if not total or total <= 0:
        filled = 0
    else:
        ratio = max(0.0, min(1.0, current / total))
        filled = int(length * ratio)
    return "█" * filled + "░" * (length - filled)


def fmt_task(t: dict, short: bool = False) -> str:
    label = LABELS.get(t.get("label", "idea"), {"emoji": "", "name": "—"})
    cat = CATEGORIES.get(t.get("category", "other"), {"emoji": "", "name": "—"})
    status = t.get("status", "pending")
    if status == STATUS_DONE:
        status_icon = "✅"
    elif is_missed(t):
        status_icon = "⚠️"
    else:
        status_icon = "⏳"
    pin_str = "📌 " if t.get("pinned") else ""

    due_dt = parse_due(t.get("due", ""))
    remain = ""
    if due_dt and status != STATUS_DONE:
        remain = "  " + time_remaining_str(due_dt)

    src = " 🤖" if t.get("source") == "ai" else ""

    lines = [
        f"{pin_str}*№{t['id']}* {label['emoji']} {status_icon}{src}",
        f"📝 {t.get('text', '')}",
        f"🕐 {t.get('due', '')}{remain}",
        f"🏷 {cat['emoji']} {cat['name']}   {label['emoji']} {label['name']}",
    ]
    subtasks = t.get("subtasks") or []
    if subtasks:
        done_n = sum(1 for s in subtasks if s.get("done"))
        lines.append(f"📝 Підзадачі: *{done_n}/{len(subtasks)}*")
    if not short:
        if t.get("postponed_count"):
            lines.append(f"🔁 Перенесено разів: *{t['postponed_count']}*")
        if status == STATUS_DONE and t.get("completed_at"):
            lines.append(f"✅ Виконано: {t['completed_at'][:16].replace('T', ' ')}")
    return "\n".join(lines)


def build_task_list_text(tasks: list, title: str) -> str:
    if not tasks:
        return f"{title}\n\n📭 Немає завдань."
    lines = [title, ""]
    for t in tasks:
        label = LABELS.get(t.get("label", "idea"), {"emoji": ""})
        status_icon = "✅" if t.get("status") == STATUS_DONE else ("⚠️" if is_missed(t) else "⏳")
        pin_str = "📌" if t.get("pinned") else ""
        lines.append(f"{status_icon} {pin_str}{label['emoji']} №{t['id']} — {t.get('due','')[-5:]} {t.get('text','')[:35]}")
    return "\n".join(lines)


def build_daily_summary_text(stats: dict, streak: int) -> str:
    lines = [
        f"📅 *Підсумок дня — {stats['date_str']}*", "",
        f"✅ Виконано\n{stats['done_count']} задач", "",
        f"❌ Не виконано\n{stats['missed_count']}", "",
    ]
    if stats["longest"]:
        seconds, text = stats["longest"]
        lines.append(f"⏱ Найдовша задача\n{fmt_duration(seconds)} ({text[:30]})")
        lines.append("")
    lines.append(f"🔥 Серія\n{streak} днів")
    if stats["postponed_count"]:
        lines.append("")
        lines.append(f"↪️ Перенесено на пізніше\n{stats['postponed_count']} задач")
    return "\n".join(lines)


def fmt_ai_plan_preview(plan: dict, selected: set) -> str:
    tasks = plan.get("tasks", [])
    total_minutes = sum(t.get("estimated_minutes", 30) for i, t in enumerate(tasks) if i in selected)
    lines = ["☀️ *AI План на сьогодні*", ""]
    if plan.get("focus"):
        lines.append(f"🎯 Головний фокус: *{plan['focus']}*")
    if plan.get("reason"):
        lines.append(f"_{plan['reason']}_")
    if plan.get("advice"):
        lines.append(f"💡 Порада: {plan['advice']}")
    lines.append("")
    lines.append("🔥 *Пріоритети:*")
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t["label"], {})
        cat = CATEGORIES.get(t["category"], {})
        lines.append(
            f"{mark} {label.get('emoji','')} *{t['time']}* — {t['text']} "
            f"({cat.get('emoji','')} {cat.get('name','')}, ~{t.get('estimated_minutes', 30)} хв)"
        )
    h, m = divmod(total_minutes, 60)
    load_parts = ([f"{h} год"] if h else []) + ([f"{m} хв"] if m else [])
    lines.append("")
    lines.append(f"📊 Заплановане навантаження: ~{' '.join(load_parts) or '0 хв'}")
    lines.append("")
    lines.append("Натисни на задачу, щоб зняти/додати позначку, потім підтверди.")
    return "\n".join(lines)


def fmt_goals_list(goals: list) -> str:
    if not goals:
        return "🎯 *Мої цілі*\n\nЩе немає жодної цілі.\nНатисни «➕ Додати ціль», щоб задати першу — AI буде враховувати її при плануванні."
    lines = ["🎯 *Мої цілі*", ""]
    for g in goals:
        status = "✅" if g.get("active") else "⏸ (неактивна)"
        pr = PRIORITY_EMOJI.get(g.get("priority", "medium"), "🟡")
        lines.append(f"{pr} *{g.get('title','')}* — {status}")
        if g.get("description"):
            lines.append(f"   _{g['description'][:80]}_")
        lines.append("")
    return "\n".join(lines).strip()


def fmt_goal_progress(g: dict) -> str:
    target = g.get("target_amount")
    current = g.get("current_amount")
    if target is None or current is None:
        return ""
    percent = int(round(current / target * 100)) if target else 0
    bar = render_progress_bar(current, target)
    remain = max(0, target - current)
    return (
        f"{bar} {percent}%\n"
        f"{current:,.0f} / {target:,.0f} грн\n"
        f"Залишилось: {remain:,.0f} грн"
    ).replace(",", " ")


def fmt_projects_list(projects: list) -> str:
    if not projects:
        return "📁 *Мої проєкти*\n\nЩе немає жодного проєкту.\nНатисни «➕ Додати проєкт», щоб створити перший — AI буде бачити прогрес і давати поради."
    lines = ["📁 *Мої проєкти*", ""]
    for p in projects:
        status = "🟢 Активний" if p.get("status") == PROJECT_ACTIVE else "✅ Завершено"
        lines.append(f"*{p.get('title','')}* — {status}")
        if p.get("description"):
            lines.append(f"   _{p['description'][:100]}_")
        lines.append("")
    return "\n".join(lines).strip()


def fmt_budget(b: dict) -> str:
    limit = b.get("limit", 0)
    spent = b.get("spent", 0)
    remain = max(0, limit - spent)
    bar = render_progress_bar(spent, limit)
    return (
        f"📦 *{b.get('title','')}*\n\n"
        f"Ліміт: {limit:,.0f} грн\n"
        f"Витрачено: {spent:,.0f} грн\n"
        f"Залишилось: {remain:,.0f} грн\n"
        f"{bar}"
    ).replace(",", " ")


def fmt_money(amount: float, currency: str = "грн") -> str:
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,.0f} {currency}".replace(",", " ")