from database import goals as goals_db


def progress_percent(goal: dict) -> int:
    if goal.get("goal_type") != "financial":
        return 0
    target = goal.get("target_amount") or 0
    if not target:
        return 0
    current = goal.get("current_amount") or 0
    return max(0, min(100, round(current / target * 100)))


def progress_bar(percent: int, length: int = 10) -> str:
    filled = round(length * percent / 100)
    return "█" * filled + "░" * (length - filled)


async def create_goal(
    uid: int,
    title: str,
    description: str = "",
    priority: str = "medium",
    goal_type: str = "general",
    target_amount: float | None = None,
    deadline: str | None = None,
) -> str:
    return await goals_db.add_goal(
        uid=uid,
        title=title,
        description=description,
        priority=priority,
        goal_type=goal_type,
        target_amount=target_amount,
        deadline=deadline,
    )


async def list_active_goals(uid: int) -> list:
    return await goals_db.get_active_goals(uid)


async def list_all_goals(uid: int) -> list:
    return await goals_db.get_all_goals(uid)


async def toggle_goal_status(gid: str, active: bool):
    await goals_db.toggle_goal(gid, active)


async def add_progress(gid: str, amount: float):
    await goals_db.add_goal_progress(gid, amount)


async def remove_goal(gid: str):
    await goals_db.delete_goal(gid)