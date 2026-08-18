import logging
from datetime import datetime, timedelta

from config.constants import STATUS_DONE
from database import goals as goals_db
from database import tasks as tasks_db
from services import ai_service

logger = logging.getLogger("scheduler.daily_jobs")

STALE_GOAL_DAYS = 14       # ціль вважається "завислою", якщо створена понад N днів тому
MIN_DONE_TASKS_NEARBY = 8  # і при цьому виконано чимало дрібних задач за той самий період


async def _is_goal_stalled(uid: int, goal: dict, all_tasks: list) -> bool:
    created_at = goal.get("created_at")
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if (datetime.now() - created).days < STALE_GOAL_DAYS:
        return False

    if goal.get("goal_type") == "financial":
        # накопичення жодного разу не зрушилось з місця
        return (goal.get("current_amount") or 0) == 0

    # звичайна ціль — дивимось, чи є виконані задачі, прив'язані до неї
    gid = str(goal.get("_id") or goal.get("id") or "")
    linked_done = [
        t for t in all_tasks
        if str(t.get("goal_id", "")) == gid and t.get("status") == STATUS_DONE
    ]
    return len(linked_done) == 0


async def detect_stalled_goal(uid: int) -> dict | None:
    """Повертає перший знайдений 'завислий' актив разом з контекстом, або None."""
    goals = await goals_db.get_active_goals(uid)
    if not goals:
        return None

    all_tasks = await tasks_db.get_user_tasks(uid)
    window_start = (datetime.now() - timedelta(days=STALE_GOAL_DAYS)).strftime("%Y-%m-%d")
    done_nearby = [
        t for t in all_tasks
        if t.get("status") == STATUS_DONE and (t.get("completed_at") or "") >= window_start
    ]

    if len(done_nearby) < MIN_DONE_TASKS_NEARBY:
        return None  # немає ознаки "тікає у дрібниці замість цілі"

    for goal in goals:
        if await _is_goal_stalled(uid, goal, all_tasks):
            return {
                "goal": goal,
                "done_nearby_count": len(done_nearby),
                "stale_days": STALE_GOAL_DAYS,
            }
    return None


async def generate_insight_text(context: dict) -> str | None:
    goal = context["goal"]
    prompt = f"""Користувач має активну ціль: "{goal.get('title','')}".
Опис: {goal.get('description','')}
Ціль створена понад {context['stale_days']} днів тому і жодного прогресу по ній не було.
За той самий час користувач виконав {context['done_nearby_count']} дрібних задач.

Напиши коротке (3-5 речень), емпатійне, але чесне спостереження українською мовою
в стилі: "Я помітив дещо цікаве. Останні N днів ти постійно відкладаєш ціль X.
При цьому на дрібні задачі витрачаєш багато часу." Заверши питанням, чи хоче
користувач розібрати причини і отримати 2-3 варіанти рішення.
Не повчай, не тисни — просто чесне спостереження від союзника."""
    return await ai_service.generate_text(prompt, temperature=0.8)


async def generate_breakdown(context: dict) -> str | None:
    goal = context["goal"]
    prompt = f"""Ціль користувача: "{goal.get('title','')}". Опис: {goal.get('description','')}.
Ця ціль зависла понад {context['stale_days']} днів без прогресу.

Запропонуй українською мовою:
1. Коротко (1-2 речення) найімовірнішу причину, чому ціль забуксувала (перевантаженість,
   нечіткість першого кроку, страх почати, невідповідний пріоритет тощо — обери найправдоподібніше).
2. Рівно 3 конкретні варіанти дій — кожен максимум одне речення, дієві й різні за підходом
   (напр. зменшити перший крок, змінити дедлайн, розбити на під-цілі).

Формат — простий текст з емодзі-маркерами, без markdown-заголовків."""
    return await ai_service.generate_text(prompt, temperature=0.7)