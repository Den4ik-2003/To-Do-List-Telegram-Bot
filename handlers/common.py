import logging
from datetime import datetime, timedelta

from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config.constants import LABELS, CATEGORIES, STATUS_DONE, STATUS_PENDING, DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import users as users_db

logger = logging.getLogger("tasks_bot")


class Auth(StatesGroup):
    waiting_password = State()


# ---- пагінація списку задач і AI-план (per uid, in-memory) ----
user_list_cache: dict = {}
ai_suggestions_cache: dict = {}


async def require_auth(msg: Message, state: FSMContext) -> bool:
    try:
        authed = await users_db.is_authorized(msg.from_user.id)
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT)
        return False
    if authed:
        return True
    current_state = await state.get_state()
    if current_state != Auth.waiting_password:
        await state.set_state(Auth.waiting_password)
        await msg.answer("🔒 *Доступ закритий*\n\nВведіть пароль:", reply_markup=ReplyKeyboardRemove())
    return False


# =========================================================
# ФОРМАТУВАННЯ ЗАДАЧ (перенесено з попередньої версії бота)
# =========================================================

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


def sort_tasks(tasks: list) -> list:
    def key(t):
        return (t.get("due", ""), LABELS.get(t.get("label", "idea"), {}).get("order", 9))
    return sorted(tasks, key=lambda t: t.get("due", ""))


def sort_tasks_by_label_then_due(tasks: list) -> list:
    order = {"urgent": 0, "medium": 1, "low": 2, "idea": 3, "personal": 4}
    return sorted(tasks, key=lambda t: (order.get(t.get("label", "idea"), 9), t.get("due", "")))


def fmt_task(t: dict, short: bool = False) -> str:
    label = LABELS.get(t.get("label", "idea"), {"emoji": "", "name": "—"})
    cat = CATEGORIES.get(t.get("category", "other"), {"emoji": "", "name": "—"})
    status = t.get("status", STATUS_PENDING)
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
    if t.get("project_id"):
        lines.append(f"📁 Проєкт задачі: див. «Мої проєкти»")
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


def fmt_duration(seconds: float) -> str:
    total_min = int(seconds // 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"


def level_progress(xp: int):
    level = 1
    total = 0
    threshold = 100
    while xp >= total + threshold:
        total += threshold
        level += 1
        threshold = 100 + level * 150
    into_level = xp - total
    return level, into_level, threshold


async def compute_daily_stats(uid: int, tasks_db) -> dict:
    today_str = datetime.now().strftime("%d.%m.%Y")
    tasks = await tasks_db.get_user_tasks(uid)
    done_today, missed_today, postponed_today = [], [], 0
    longest = None
    for t in tasks:
        due = t.get("due", "")
        if not due.startswith(today_str):
            continue
        if t.get("status") == STATUS_DONE:
            done_today.append(t)
            created = t.get("created_at")
            completed = t.get("completed_at")
            if created and completed:
                try:
                    delta = (datetime.fromisoformat(completed) - datetime.fromisoformat(created)).total_seconds()
                    if longest is None or delta > longest[0]:
                        longest = (delta, t.get("text", ""))
                except ValueError:
                    pass
        elif is_missed(t):
            missed_today.append(t)
        if t.get("postponed_today"):
            postponed_today += 1

    return {
        "date_str": today_str,
        "done_count": len(done_today),
        "missed_count": len(missed_today),
        "postponed_count": postponed_today,
        "longest": longest,
    }