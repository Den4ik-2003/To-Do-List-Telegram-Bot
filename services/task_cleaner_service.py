"""
services/task_cleaner_service.py

🧹 AI-прибиральник: раз на тиждень знаходить PENDING-задачі, які давно
"зависли" без руху, і пропонує користувачу видалити їх одним списком.

ВАЖЛИВО: сам критерій "застаріла задача" — чисто алгоритмічний (вік
задачі), а НЕ рішення AI-моделі. Це навмисно: якщо прив'язати сам факт
пошуку до AI-виклику, фіча відмовить точно так само мовчки, як щойно
було з Threads, коли AI_API_KEY недоступний. AI (якщо доступний)
використовується лише ОПЦІОНАЛЬНО — щоб додати одне дружнє речення
перед списком; якщо AI недоступний або впав, просто надсилається список
без коментаря, сама фіча не ламається.

Задача вважається "застарілою", якщо:
- вона НЕ закріплена (pinned) — закріплені користувач явно хоче бачити;
- і або: є термін (due), і він прострочений на STALE_DAYS+ днів;
- або: терміну немає, а задача створена STALE_DAYS+ днів тому.
"""

import logging
from datetime import datetime

from database import tasks as tasks_db
from services import ai_service
from utils.dates import parse_due

logger = logging.getLogger("tasks_bot")

MAX_LISTED_IN_MESSAGE = 15


def _is_stale(t: dict, threshold_days: int, now: datetime) -> bool:
    if t.get("pinned"):
        return False

    due = parse_due(t.get("due", ""))
    if due:
        return (now - due).days >= threshold_days

    created_raw = t.get("created_at")
    if not created_raw:
        return False
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return False
    return (now - created).days >= threshold_days


async def find_stale_tasks_by_user(threshold_days: int) -> dict[int, list[dict]]:
    """Повертає {uid: [задача, ...]} лише для користувачів, у кого є хоч
    одна застаріла задача. Використовує вже наявний tasks_db.find_pending()
    (той самий, яким користуються reminder_task/midnight_rollover_task),
    а не нову функцію в database-шарі — щоб не дублювати доступ до БД."""
    pending = await tasks_db.find_pending()
    now = datetime.now()
    by_uid: dict[int, list[dict]] = {}
    for t in pending:
        if _is_stale(t, threshold_days, now):
            by_uid.setdefault(t["uid"], []).append(t)
    return by_uid


async def build_cleaner_intro(count: int) -> str:
    """Опціональний AI-коментар одним реченням. Якщо AI недоступний або
    впав — повертає порожній рядок, виклик просто пропускає його."""
    if not ai_service.is_available():
        return ""
    prompt = (
        f"Користувач бота-планувальника має {count} задач(і), які давно не виконані "
        "(прострочені або без терміну і давно створені). Напиши ОДНЕ коротке дружнє "
        "речення українською, яке м'яко звертає на це увагу, без осуду і повчань. "
        "Максимум 1 emoji в кінці речення. Поверни лише саме речення, без лапок."
    )
    try:
        text = await ai_service.generate_text(prompt, temperature=0.7)
        return (text or "").strip()
    except Exception:
        logger.exception("task_cleaner_service: не вдалось згенерувати вступну фразу")
        return ""


def format_cleaner_digest(tasks: list[dict], intro: str = "") -> str:
    lines = ["🧹 *AI-прибиральник*", ""]
    if intro:
        lines.append(intro)
        lines.append("")
    lines.append(f"Знайшов {len(tasks)} задач(і), які давно висять без руху:")
    lines.append("")
    for t in tasks[:MAX_LISTED_IN_MESSAGE]:
        due_str = t.get("due") or "без терміну"
        text = (t.get("text") or "")[:40]
        lines.append(f"• №{t['id']} — {text} ({due_str})")
    if len(tasks) > MAX_LISTED_IN_MESSAGE:
        lines.append(f"…і ще {len(tasks) - MAX_LISTED_IN_MESSAGE}")
    lines.append("")
    lines.append("Що зробити?")
    return "\n".join(lines)


async def delete_tasks(task_ids: list[int]) -> int:
    deleted = 0
    for tid in task_ids:
        try:
            await tasks_db.delete_task(tid)
            deleted += 1
        except Exception:
            logger.exception("task_cleaner_service: не вдалось видалити задачу %s", tid)
    return deleted