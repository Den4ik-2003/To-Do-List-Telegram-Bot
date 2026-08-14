from database import projects as projects_db
from database import tasks as tasks_db


def budget_percent(project: dict) -> int:
    budget = project.get("budget") or 0
    if not budget:
        return 0
    spent = project.get("spent") or 0
    return max(0, min(100, round(spent / budget * 100)))


async def create_project(
    uid: int,
    title: str,
    description: str = "",
    deadline: str | None = None,
    budget: float | None = None,
    goal_id: str | None = None,
) -> str:
    return await projects_db.add_project(uid, title, description, deadline, budget, goal_id)


async def list_active_projects(uid: int) -> list:
    return await projects_db.get_active_projects(uid)


async def list_all_projects(uid: int) -> list:
    return await projects_db.get_all_projects(uid)


async def get_project_progress(uid: int, project_id: str) -> dict:
    tasks = await tasks_db.get_project_tasks(uid, project_id)
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "done")
    percent = round(done / total * 100) if total else 0
    return {"total": total, "done": done, "percent": percent}


async def toggle_project_status(pid: str, active: bool):
    await projects_db.set_project_status(pid, "active" if active else "done")


async def remove_project(pid: str):
    await projects_db.delete_project(pid)