import logging
from datetime import datetime

from config.constants import LABELS, CATEGORIES, DEFAULT_CURRENCY
from config.settings import WORK_HOURS_TEXT, AI_DAILY_LIMIT
from database import tasks as tasks_db
from database import goals as goals_db
from database import projects as projects_db
from database import finances as finances_db
from database import users as users_db
from database import ai_usage as ai_usage_db
from services import ai_service

logger = logging.getLogger("tasks_bot")

STATUS_PENDING = "pending"
STATUS_DONE = "done"


def _parse_due(due_str: str):
    try:
        return datetime.strptime(due_str, "%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return None


def _is_missed(t: dict) -> bool:
    if t.get("status") != STATUS_PENDING:
        return False
    due = _parse_due(t.get("due", ""))
    return bool(due and due <= datetime.now())


def _project_stage_line(p: dict) -> str:
    """
    Додає до опису проєкту інформацію про поточний етап (перший ще не
    завершений), щоб AI генерував задачі, які просувають саме цей етап,
    а не абстрактні задачі по проєкту в цілому.
    """
    stages = p.get("stages") or []
    if not stages:
        return ""
    current = projects_db.get_current_stage(p)
    done, total = projects_db.stage_progress(p)
    if current:
        desc = f": {current.get('description','')[:120]}" if current.get("description") else ""
        return f" | Етап {done + 1}/{total} — «{current.get('title','')}»{desc}"
    return f" | Усі {total} етапів завершено"


async def check_ai_limit(uid: int) -> tuple[bool, int]:
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    return remaining > 0, remaining


async def _build_context(uid: int) -> dict:
    active = await tasks_db.get_user_tasks(uid, statuses=[STATUS_PENDING])
    active = sorted(active, key=lambda t: t.get("due", ""))[:20]
    done_all = await tasks_db.get_user_tasks(uid, statuses=[STATUS_DONE])
    today_key = datetime.now().strftime("%Y-%m-%d")
    done_today = [t for t in done_all if (t.get("completed_at") or "").startswith(today_key)]
    overdue = [t for t in active if _is_missed(t)]

    goals = await goals_db.get_active_goals(uid)
    projects = await projects_db.get_active_projects(uid)
    state = await users_db.get_user_state(uid)
    balance = await finances_db.get_balance(uid)

    active_text = "\n".join(
        f"- {t.get('text','')} | {t.get('due','')} | "
        f"{LABELS.get(t.get('label',''), {}).get('name','')} | "
        f"{CATEGORIES.get(t.get('category',''), {}).get('name','')}"
        for t in active
    ) or "(активних задач немає)"

    done_text = "\n".join(f"- {t.get('text','')}" for t in done_today) or "(ще нічого не виконано)"

    goals_text = "\n".join(
        f"- [{g.get('priority','medium')}] {g.get('title','')}"
        + (f" — {g.get('current_amount', 0)}/{g.get('target_amount')} {DEFAULT_CURRENCY}"
           if g.get("goal_type") == "financial" and g.get("target_amount") else "")
        + (f" — {g.get('description','')[:80]}" if g.get("description") else "")
        for g in goals
    ) or "(довгострокові цілі не задані)"

    projects_text = "\n".join(
        f"- {p.get('title','')}"
        + (f" — {p.get('description','')[:100]}" if p.get("description") else "")
        + (f" — бюджет {p.get('spent', 0)}/{p.get('budget')} {DEFAULT_CURRENCY}" if p.get("budget") else "")
        + _project_stage_line(p)
        for p in projects
    ) or "(активних проєктів немає)"

    return {
        "active_text": active_text,
        "done_text": done_text,
        "overdue_count": len(overdue),
        "goals_text": goals_text,
        "projects_text": projects_text,
        "balance": balance,
        "state": state,
    }


async def generate_daily_plan(uid: int) -> dict | None:
    if not ai_service.is_available():
        return None
    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return None

    ctx = await _build_context(uid)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    prompt = f"""Ти — персональний AI-планувальник задач. Відповідай виключно українською.

Поточний час: {now_str}
Робочий графік користувача (основна робота): {WORK_HOURS_TEXT}

Активні задачі користувача:
{ctx['active_text']}

Вже виконано сьогодні:
{ctx['done_text']}

Прострочених активних задач: {ctx['overdue_count']}
Серія (streak): {ctx['state'].get('streak', 0)} днів
XP: {ctx['state'].get('xp', 0)}
Поточний баланс: {ctx['balance']} {DEFAULT_CURRENCY}

Довгострокові цілі користувача (від найважливіших):
{ctx['goals_text']}

Активні проєкти користувача (окремі напрямки роботи, не обов'язково пов'язані із задачами напряму).
Якщо у проєкту вказано "Етап X/Y" — це поточний етап, над яким зараз реально
працює користувач; генеруй задачі, що просувають САМЕ цей етап (за його
описом), а не абстрактні задачі по проєкту в цілому:
{ctx['projects_text']}

Запропонуй 3-6 НОВИХ конкретних задач на сьогодні, реалістичних для виконання за день.
Правила:
- Не дублюй активні задачі.
- Задачі мають бути конкретними, а не абстрактними.
- Якщо у проєкту є поточний етап — хоча б одна задача має напряму просувати саме цей етап.
- Врахуй робочий графік: задачі про IT/навчання/пошук роботи став до початку або після завершення робочого дня, якщо немає інших вказівок.
- Не став дві задачі на однаковий час і не став задачу поверх уже запланованої активної задачі.
- Балансуй між напрямками (проєкти, робота, фінанси, особисте) — не роби весь план лише про одне.
- Якщо є фінансова ціль або бюджет проєкту — врахуй це при виборі фокуса дня.

Поверни ВИКЛЮЧНО валідний JSON без жодного тексту навколо, без коментарів, без markdown-розмітки (без ```), у форматі:
{{
  "focus": "короткий головний фокус дня",
  "reason": "одне речення чому саме такий фокус",
  "advice": "одна коротка порада щодо активних проєктів, цілей або фінансів",
  "tasks": [
    {{"text": "конкретна дія", "label": "urgent|medium|low|idea|personal", "category": "work|finance|home|sport|study|other", "time": "гг:хх", "estimated_minutes": 30}}
  ]
}}"""

    data = await ai_service.generate_json(prompt)
    if not data:
        return None

    tasks_out = []
    for it in data.get("tasks", []):
        if not isinstance(it, dict):
            continue
        label = it.get("label") if it.get("label") in LABELS else "medium"
        category = it.get("category") if it.get("category") in CATEGORIES else "other"
        time_str = str(it.get("time", "12:00")).strip()
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            time_str = "12:00"
        try:
            est = int(it.get("estimated_minutes", 30))
        except (TypeError, ValueError):
            est = 30
        est = max(5, min(est, 240))
        text = str(it.get("text") or "").strip()[:200]
        if not text:
            continue
        tasks_out.append({
            "text": text, "label": label, "category": category,
            "time": time_str, "estimated_minutes": est,
        })

    if not tasks_out:
        return None

    await ai_usage_db.increment_usage(uid)

    return {
        "focus": str(data.get("focus", "")).strip()[:120],
        "reason": str(data.get("reason", "")).strip()[:250],
        "advice": str(data.get("advice", "")).strip()[:250],
        "tasks": tasks_out[:6],
    }


async def generate_daily_analysis(uid: int, daily_stats: dict) -> str | None:
    if not ai_service.is_available():
        return None
    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return None

    state = await users_db.get_user_state(uid)
    projects = await projects_db.get_active_projects(uid)
    projects_text = "\n".join(
        f"- {p.get('title','')}"
        + (f" — {p.get('description','')[:100]}" if p.get("description") else "")
        + _project_stage_line(p)
        for p in projects
    ) or "(активних проєктів немає)"

    prompt = f"""Ти — персональний AI-аналітик продуктивності. Відповідай українською, коротко (до 150 слів), без формальних компліментів.

Дані за сьогодні:
Виконано задач: {daily_stats.get('done_count', 0)}
Пропущено (прострочено): {daily_stats.get('missed_count', 0)}
Перенесено на пізніше: {daily_stats.get('postponed_count', 0)}
Серія (streak): {state.get('streak', 0)} днів
XP: {state.get('xp', 0)}

Активні проєкти користувача (з поточним етапом, якщо він є):
{projects_text}

Дай короткий, конкретний і корисний аналіз у форматі (українською, збережи ці підзаголовки):
🎯 Найкраще: ...
⚠️ Проблема: ...
📁 Проєкти: коментар щодо прогресу активних проєктів і поточних етапів, що варто зробити далі
💡 Що змінити завтра: ..."""

    text = await ai_service.generate_text(prompt)
    if not text:
        return None
    await ai_usage_db.increment_usage(uid)
    return text


async def generate_weekly_analysis(uid: int, weekly_stats: dict) -> str | None:
    if not ai_service.is_available():
        return None
    allowed, _ = await check_ai_limit(uid)
    if not allowed:
        return None

    prompt = f"""Ти — персональний AI-аналітик продуктивності. Відповідай українською, коротко (до 200 слів).

Підсумок тижня користувача:
Виконано задач: {weekly_stats.get('done_count', 0)}
Пропущено задач: {weekly_stats.get('missed_count', 0)}
Дохід за тиждень: {weekly_stats.get('income', 0)} {DEFAULT_CURRENCY}
Витрати за тиждень: {weekly_stats.get('expense', 0)} {DEFAULT_CURRENCY}
Прогрес по фінансових цілях: {weekly_stats.get('goals_progress_text', '(даних немає)')}
Активних проєктів: {weekly_stats.get('active_projects_count', 0)}

Дай підсумок тижня у форматі (українською, збережи підзаголовки):
🏆 Що зроблено добре: ...
⚠️ Що просіло: ...
🎯 Що зробити наступного тижня: ..."""

    text = await ai_service.generate_text(prompt)
    if not text:
        return None
    await ai_usage_db.increment_usage(uid)
    return text