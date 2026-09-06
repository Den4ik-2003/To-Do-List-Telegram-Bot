"""
ЗМІНЕНИЙ ФАЙЛ: services/project_service.py

Це фінальна версія, що замінює тимчасовий hotfix (hasattr-заглушку) і
чужий чернетковий варіант з generate_stages_ai/ask_ai.

Виправлено:
1. get_project_progress() тепер реально працює — database/tasks.py вже
   містить get_project_tasks(uid, project_id) (окремий файл, онови теж).
   Заглушку hasattr(...) прибрано, вона більше не потрібна.
2. AI-генерація етапів переписана на РЕАЛЬНИЙ клієнт бота
   services/ai_service.py (той самий, що вже юзають ai_chat.py та
   kitchen_service.py) — generate_json(prompt). Функції ask_ai у проєкті
   НЕМАЄ і ніколи не було, тож попередній варіант (import ask_ai) завжди
   падав би на ImportError і мовчки повертав [].

Залишено обидві назви для стадій AI-генерації — generate_stages_ai() і
save_ai_stages() — так, як вони вже названі у файлі, який ти кинув
останнім, щоб не було розсинхрону з тим, що вже могло бути написано в
handlers/projects.py під ці імена.
"""

import logging

from database import projects as projects_db
from database import tasks as tasks_db
from services import ai_service

logger = logging.getLogger("tasks_bot")


STAGE_STATUS_LABELS = {
    "pending": "⏳ Очікує",
    "in_progress": "🔵 В процесі",
    "done": "✅ Завершено",
}
STAGE_STATUS_ICONS = {k: v.split()[0] for k, v in STAGE_STATUS_LABELS.items()}


def stage_status_label(status: str) -> str:
    return STAGE_STATUS_LABELS.get(status, STAGE_STATUS_LABELS["pending"])


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
# ЕТАПИ ПРОЄКТУ — CRUD
# =========================================================

async def move_stage(pid: str, stage_id: str, direction: str):
    await projects_db.move_stage(pid, stage_id, direction)


async def edit_stage(pid: str, stage_id: str, title: str, description: str):
    await projects_db.update_stage(pid, stage_id, {"title": title, "description": description})


async def set_stage_status(pid: str, stage_id: str, status: str):
    await projects_db.update_stage(pid, stage_id, {"status": status})


# =========================================================
# ✨ AI-ГЕНЕРАЦІЯ ЕТАПІВ ПРОЄКТУ
# =========================================================
# Використовує services/ai_service.generate_json — той самий AI-клієнт
# (OpenAI-сумісний, AI_API_KEY/AI_BASE_URL/AI_MODEL з config/settings.py),
# яким уже користуються ai_chat.py, ai_planner та kitchen_service.py.
# Ніякого окремого/нового AI-клієнта тут не створюється.

async def generate_stages_ai(title: str, description: str) -> list[dict]:
    if not ai_service.is_available():
        logger.warning("generate_stages_ai: AI недоступний (немає AI_API_KEY)")
        return []

    desc_part = f' Опис проєкту: "{description}".' if description else ""
    prompt = (
        f'Проєкт користувача: "{title}".{desc_part}\n'
        "Запропонуй від 3 до 6 логічних послідовних етапів реалізації цього "
        "проєкту. Кожен етап має мати коротку зрозумілу назву (до 6 слів) та "
        "короткий опис (1-2 речення) того, що саме треба зробити. Етапи мають "
        "йти в реалістичному хронологічному порядку від початку до завершення.\n"
        'Поверни ЛИШЕ JSON: {"stages": [{"title": "назва етапу", "description": "опис"}]}'
    )

    try:
        data = await ai_service.generate_json(prompt, temperature=0.6)
    except Exception:
        logger.exception("generate_stages_ai: помилка виклику AI")
        return []

    if not data or not isinstance(data.get("stages"), list):
        logger.warning("generate_stages_ai: AI повернув порожній або невалідний результат: %r", data)
        return []

    result = []
    for s in data["stages"]:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        result.append({
            "title": str(s["title"])[:100],
            "description": str(s.get("description", ""))[:400],
        })
    return result[:8]


async def save_ai_stages(pid: str, stages: list[dict]) -> None:
    await projects_db.add_stages_bulk(pid, stages)


# Аліас на випадок, якщо десь у хендлерах уже викликається інша назва
# з попередньої чернетки цього ж модуля — обидва імені ведуть до одного коду.
confirm_ai_stages = save_ai_stages
generate_stages_with_ai = generate_stages_ai