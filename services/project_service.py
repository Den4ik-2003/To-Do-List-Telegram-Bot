import json
import logging

from database import projects as projects_db
from database import tasks as tasks_db

logger = logging.getLogger("tasks_bot")


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


# =========================================================
# ✨ AI-ГЕНЕРАЦІЯ ЕТАПІВ ПРОЄКТУ
# =========================================================
# УВАГА: `ask_ai` нижче — ЗАГЛУШКА-КОНТРАКТ. Я не бачив services/ai_service.py
# (чи де саме у вас лежить клієнт до Gemini/OpenRouter, який уже юзає ai_planner),
# тож імпорт може не збігтись 1-в-1. Функція зроблена так, щоб не валити бота,
# якщо клієнта ще нема: просто поверне [] і залогує помилку.
# Скинь мені ai_service.py (або ai_planner_service.py) — підправлю імпорт/сигнатуру за 1 рядок.

async def generate_stages_ai(title: str, description: str) -> list[dict]:
    try:
        from services.ai_service import ask_ai  # TODO: підтвердити реальний шлях і сигнатуру
    except ImportError:
        logger.error("generate_stages_ai: не знайдено services.ai_service.ask_ai — AI-клієнт не підключено")
        return []

    prompt = (
        "Ти — асистент з планування проєктів. Ось проєкт користувача:\n"
        f"Назва: {title}\n"
        f"Опис: {description or '(без опису)'}\n\n"
        "Запропонуй від 3 до 6 логічних послідовних етапів виконання цього проєкту.\n"
        "Відповідай ЛИШЕ у форматі JSON-масиву, без жодного тексту навколо:\n"
        '[{"title": "...", "description": "..."}, ...]\n'
        "title — до 6 слів, description — 1-2 короткі речення."
    )

    try:
        raw = await ask_ai(prompt)
    except Exception:
        logger.exception("generate_stages_ai: помилка виклику AI")
        return []

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        stages = json.loads(cleaned)
    except Exception:
        logger.warning("generate_stages_ai: AI повернув невалідний JSON: %r", raw)
        return []

    if not isinstance(stages, list):
        return []

    result = []
    for s in stages:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        result.append({
            "title": str(s["title"])[:100],
            "description": str(s.get("description", ""))[:400],
        })
    return result[:8]


async def save_ai_stages(pid: str, stages: list[dict]) -> None:
    await projects_db.add_stages_bulk(pid, stages)