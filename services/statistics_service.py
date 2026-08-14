from datetime import datetime, timedelta

from config.settings import AI_DAILY_LIMIT
from database import tasks as tasks_db
from database import goals as goals_db
from database import projects as projects_db
from database import finances as finances_db
from database import ai_usage as ai_usage_db

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


async def get_overview(uid: int) -> dict:
    tasks = await tasks_db.get_user_tasks(uid)
    active = [t for t in tasks if t.get("status") == STATUS_PENDING]
    done_all = [t for t in tasks if t.get("status") == STATUS_DONE]
    overdue = [t for t in active if _is_missed(t)]

    goals = await goals_db.get_active_goals(uid)
    projects = await projects_db.get_active_projects(uid)
    balance = await finances_db.get_balance(uid)
    remaining_ai = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)

    return {
        "tasks_active": len(active),
        "tasks_done": len(done_all),
        "tasks_overdue": len(overdue),
        "goals_active": len(goals),
        "projects_active": len(projects),
        "balance": balance,
        "ai_remaining": remaining_ai,
        "ai_limit": AI_DAILY_LIMIT,
    }


async def get_weekly_summary(uid: int) -> dict:
    tasks = await tasks_db.get_user_tasks(uid)
    week_ago = datetime.now() - timedelta(days=7)

    done_count = 0
    missed_count = 0
    for t in tasks:
        completed_at = t.get("completed_at")
        if t.get("status") == STATUS_DONE and completed_at:
            try:
                if datetime.fromisoformat(completed_at) >= week_ago:
                    done_count += 1
            except ValueError:
                pass
        elif _is_missed(t):
            due = _parse_due(t.get("due", ""))
            if due and due >= week_ago:
                missed_count += 1

    start = week_ago.strftime("%Y-%m-%dT00:00:00")
    end = datetime.now().strftime("%Y-%m-%dT23:59:59")
    finance_summary = await finances_db.get_period_summary(uid, start, end)

    goals = await goals_db.get_active_goals(uid)
    goals_progress_text = ", ".join(
        f"{g.get('title','')}: {g.get('current_amount', 0)}/{g.get('target_amount')}"
        for g in goals if g.get("goal_type") == "financial" and g.get("target_amount")
    ) or "(даних немає)"

    projects = await projects_db.get_active_projects(uid)

    return {
        "done_count": done_count,
        "missed_count": missed_count,
        "income": finance_summary["income"],
        "expense": finance_summary["expense"],
        "net": finance_summary["net"],
        "goals_progress_text": goals_progress_text,
        "active_projects_count": len(projects),
    }