"""
НОВИЙ ФАЙЛ: services/reschedule_service.py

Логіка фічі "Автоперенесення задач". Перенесення ЗАВЖДИ означає зміну
поля `due` вже існуючого документа задачі через tasks_db.update_task() —
нова задача НІКОЛИ не створюється (захист від дублювання).

Правило "вільного дня" (просте, бо персистентних даних про заплановані
вільні години на МАЙБУТНІ дні в боті немає — є лише одноразовий ввід
"скільки часу маєш сьогодні" в AI Планері, який живе лише в оперативній
пам'яті на час однієї генерації, не зберігається в БД на майбутнє):

  1. Кількість PENDING-задач на день < MAX_TASKS_PER_DAY.
  2. Якщо в задачі, яку переносимо, є estimated_minutes — додатково
     сума estimated_minutes усіх PENDING-задач того дня + ця задача
     не повинна перевищувати MAX_MINUTES_PER_DAY.

Обидва пороги — у config/settings.py, керуються через .env.
"""

import logging
from datetime import datetime, date, timedelta

from config.settings import MAX_TASKS_PER_DAY, MAX_MINUTES_PER_DAY
from config.constants import STATUS_PENDING, STATUS_DONE
from database import tasks as tasks_db
from database import users as users_db
from utils.dates import parse_due, fmt_due

logger = logging.getLogger("tasks_bot")

MAX_DAYS_LOOKAHEAD = 60


async def _day_load(uid: int, exclude_tid: int | None = None) -> tuple[dict[date, int], dict[date, int]]:
    """Кількість задач і сумарні хвилини по днях для всіх PENDING задач користувача."""
    tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING])
    counts: dict[date, int] = {}
    minutes: dict[date, int] = {}
    for t in tasks:
        if exclude_tid is not None and t.get("id") == exclude_tid:
            continue
        due = parse_due(t.get("due", ""))
        if not due:
            continue
        d = due.date()
        counts[d] = counts.get(d, 0) + 1
        minutes[d] = minutes.get(d, 0) + (t.get("estimated_minutes") or 0)
    return counts, minutes


async def find_next_free_day(
    uid: int,
    start_date: date,
    exclude_tid: int | None = None,
    estimated_minutes: int | None = None,
    day_counts: dict[date, int] | None = None,
    day_minutes: dict[date, int] | None = None,
) -> date:
    """Найближчий, починаючи з start_date (включно), не перевантажений день.

    day_counts/day_minutes можна передати ззовні — використовується при
    масовому перенесенні (reschedule_many), щоб кожна наступна задача
    враховувала вже "заброньовані" в межах цього ж прогону дні і кілька
    задач не звалились на один і той самий перший вільний день."""
    if day_counts is None or day_minutes is None:
        day_counts, day_minutes = await _day_load(uid, exclude_tid=exclude_tid)

    day = start_date
    for _ in range(MAX_DAYS_LOOKAHEAD):
        count_ok = day_counts.get(day, 0) < MAX_TASKS_PER_DAY
        minutes_ok = True
        if estimated_minutes:
            minutes_ok = day_minutes.get(day, 0) + estimated_minutes <= MAX_MINUTES_PER_DAY
        if count_ok and minutes_ok:
            return day
        day += timedelta(days=1)

    logger.warning(
        "find_next_free_day: не знайдено вільного дня за %d днів для uid=%s, беремо останній перевірений",
        MAX_DAYS_LOOKAHEAD, uid,
    )
    return day


async def reschedule_task_to_next_free_day(uid: int, tid: int, *, auto: bool = False) -> date | None:
    """Переносить ОДНУ вже існуючу задачу на найближчий вільний день.
    Мітка, категорія, опис, проєкт, пріоритет — не змінюються, лише due.
    Повертає нову дату, або None, якщо задачу не знайдено."""
    t = await tasks_db.get_task(tid)
    if not t:
        return None

    old_due = parse_due(t.get("due", ""))
    time_part = old_due.strftime("%H:%M") if old_due else "09:00"
    # пошук починаємо із завтра — переносити прострочену задачу "знову на
    # сьогодні" не має сенсу
    start = date.today() + timedelta(days=1)

    new_day = await find_next_free_day(
        uid, start, exclude_tid=tid, estimated_minutes=t.get("estimated_minutes")
    )
    new_due_dt = datetime.strptime(f"{new_day.strftime('%d.%m.%Y')} {time_part}", "%d.%m.%Y %H:%M")

    await tasks_db.update_task(tid, {
        "due": fmt_due(new_due_dt),
        "status": STATUS_PENDING,
        "reminded_before": False,
        "missed_flagged": False,
        "postponed_count": t.get("postponed_count", 0) + 1,
        "postponed_today": True,
        "auto_rescheduled": bool(auto),
        "last_reschedule_at": datetime.now().isoformat(),
    })

    state = await users_db.get_user_state(uid)
    await users_db.save_user_state(uid, {"total_postponed": state.get("total_postponed", 0) + 1})
    return new_day


async def reschedule_many(uid: int, task_ids: list[int]) -> dict[int, date]:
    """Масове перенесення («📅 Перенести всі») — кожна наступна задача
    враховує вже зайняті в цьому ж прогоні дні, щоб не перевантажити
    один день."""
    day_counts, day_minutes = await _day_load(uid)
    result: dict[int, date] = {}

    for tid in task_ids:
        t = await tasks_db.get_task(tid)
        if not t:
            continue
        old_due = parse_due(t.get("due", ""))
        time_part = old_due.strftime("%H:%M") if old_due else "09:00"
        start = date.today() + timedelta(days=1)

        new_day = await find_next_free_day(
            uid, start, exclude_tid=tid,
            estimated_minutes=t.get("estimated_minutes"),
            day_counts=day_counts, day_minutes=day_minutes,
        )
        day_counts[new_day] = day_counts.get(new_day, 0) + 1
        day_minutes[new_day] = day_minutes.get(new_day, 0) + (t.get("estimated_minutes") or 0)

        new_due_dt = datetime.strptime(f"{new_day.strftime('%d.%m.%Y')} {time_part}", "%d.%m.%Y %H:%M")
        await tasks_db.update_task(tid, {
            "due": fmt_due(new_due_dt),
            "status": STATUS_PENDING,
            "reminded_before": False,
            "missed_flagged": False,
            "postponed_count": t.get("postponed_count", 0) + 1,
            "postponed_today": True,
            "auto_rescheduled": True,
            "last_reschedule_at": datetime.now().isoformat(),
        })
        result[tid] = new_day

    if result:
        state = await users_db.get_user_state(uid)
        await users_db.save_user_state(uid, {"total_postponed": state.get("total_postponed", 0) + len(result)})

    return result


async def get_reschedule_stats(uid: int) -> dict:
    """Для майбутнього підключення в '📊 Статистика' (я не бачив
    handlers/statistics.py, тож нікуди не вписую автоматично — просто
    готова функція). Рахується на льоту з поточного стану задач, а не
    окремим лічильником, що росте вічно — щоб цифра відображала РЕАЛЬНИЙ
    поточний стан, а не історичний підсумок."""
    all_tasks = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_DONE])
    rescheduled = [t for t in all_tasks if (t.get("postponed_count") or 0) > 0]
    done_after = [t for t in rescheduled if t.get("status") == STATUS_DONE]
    still_pending = [t for t in rescheduled if t.get("status") == STATUS_PENDING]
    return {
        "rescheduled": len(rescheduled),
        "done_after_reschedule": len(done_after),
        "still_pending": len(still_pending),
    }