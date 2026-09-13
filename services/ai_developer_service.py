"""
НОВИЙ ФАЙЛ: services/ai_developer_service.py

Логіка фічі "👨‍💻 AI Developer". НІЧОГО не дублює існуючу GitHub-інфраструктуру:
- Читання репозиторію відбувається через ті самі функції, що й Download ZIP
  (services/github_api.get_tree_recursive / get_blob_content / _headers) —
  просто інший фільтр файлів (текстові код-файли замість "усе підряд").
- Застосування змін іде через github_api.deploy_files — ТОЙ САМИЙ шлях,
  яким користувач деплоїть ZIP. Жодного окремого git-клієнта чи нового
  способу писати в GitHub тут немає.
- AI-виклики йдуть через services/ai_service (generate_text/generate_json) —
  той самий клієнт/ретраї/ліміти моделі, що й у planner_service.
"""

import logging

import aiohttp

from services import ai_service, github_api

logger = logging.getLogger("tasks_bot")

# Розширення, які має сенс віддавати AI як "код" (текстові файли).
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".html", ".css", ".scss",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt", ".sql", ".env.example",
    ".java", ".kt", ".go", ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".sh",
    ".xml", ".gradle", ".dockerfile",
}
IGNORED_PREFIXES = (
    "node_modules/", ".git/", "dist/", "build/", "__pycache__/",
    "venv/", ".venv/", ".next/", "coverage/",
)

# Обмеження контексту для промпту — щоб не впертись у ліміт токенів моделі
# і не роздувати один AI-запит до неадекватного розміру.
MAX_CONTEXT_CHARS = 45000
MAX_FILE_CHARS = 6000
MAX_FILES_IN_CONTEXT = 40


def _is_relevant_path(path: str) -> bool:
    if any(path.startswith(p) for p in IGNORED_PREFIXES):
        return False
    fname = path.rsplit("/", 1)[-1]
    if "." not in fname:
        return False
    ext = "." + fname.rsplit(".", 1)[-1].lower()
    return ext in CODE_EXTENSIONS


async def fetch_repo_snapshot(token: str, owner: str, repo: str, branch: str) -> dict | None:
    """Дістає дерево файлів гілки і вміст обмеженої кількості найменших
    релевантних код-файлів (щоб контекст для AI був репрезентативним, але
    не завеликим). Повертає None, якщо гілку/дерево не вдалось отримати."""
    branches = await github_api.list_branches(token, owner, repo)
    branch_sha = next((b["sha"] for b in branches if b["name"] == branch), None)
    if not branch_sha:
        return None

    tree = await github_api.get_tree_recursive(token, owner, repo, branch_sha)
    if not tree:
        return None

    all_paths = [it["path"] for it in tree["items"]]
    relevant = [it for it in tree["items"] if _is_relevant_path(it["path"])]
    relevant.sort(key=lambda i: i.get("size", 0))  # спочатку маленькі файли — більше поміститься
    relevant = relevant[:MAX_FILES_IN_CONTEXT]

    files_text: dict[str, str] = {}
    total_chars = 0
    async with aiohttp.ClientSession(headers=github_api._headers(token)) as session:
        for item in relevant:
            if total_chars >= MAX_CONTEXT_CHARS:
                break
            content = await github_api.get_blob_content(session, owner, repo, item["sha"])
            if content is None:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue  # бінарний файл — пропускаємо, AI-контексту з нього не буде
            text = text[:MAX_FILE_CHARS]
            files_text[item["path"]] = text
            total_chars += len(text)

    return {
        "commit_sha": branch_sha,
        "tree_paths": all_paths,
        "files_text": files_text,
    }


def _format_context(snapshot: dict) -> str:
    tree_lines = "\n".join(f"- {p}" for p in snapshot["tree_paths"][:250])
    files_block = "\n\n".join(
        f"### {path}\n```\n{content}\n```" for path, content in snapshot["files_text"].items()
    ) or "(жодного текстового файлу не вдалось прочитати)"
    return (
        f"Структура репозиторію (усі шляхи):\n{tree_lines}\n\n"
        f"Вміст ключових файлів (не всі — див. структуру вище):\n\n{files_block}"
    )


async def analyze_project(snapshot: dict) -> str | None:
    context = _format_context(snapshot)
    prompt = f"""Ти — AI-розробник, що аналізує чужий репозиторій. Відповідай українською.

{context}

Дай стислий, конкретний аналіз проєкту у форматі (збережи підзаголовки):
📁 Структура: коротко, як організований проєкт
🛠 Технології: стек, фреймворки, мова
⚠️ Проблемні місця: 2-4 конкретні речі, які варто покращити
💡 Рекомендація: одна головна порада, з чого почати"""
    return await ai_service.generate_text(prompt, temperature=0.4)


async def find_bugs(snapshot: dict) -> str | None:
    context = _format_context(snapshot)
    prompt = f"""Ти — AI-розробник, що шукає баги в чужому коді. Відповідай українською.

{context}

Знайди потенційні баги та логічні помилки в показаному коді. Для кожного:
- файл і приблизне місце (функція/клас)
- у чому проблема
- чому це може зламатись

Якщо явних багів не видно — так і напиши, не вигадуй проблеми. Максимум 6 пунктів."""
    return await ai_service.generate_text(prompt, temperature=0.3)


async def security_check(snapshot: dict) -> str | None:
    context = _format_context(snapshot)
    prompt = f"""Ти — AI security-аудитор. Відповідай українською.

{context}

Перевір показаний код на очевидні проблеми безпеки: захардкоджені секрети/ключі,
SQL/command injection, небезпечна десеріалізація, відсутність валідації вводу,
слабка авторизація, небезпечні залежності тощо.

Для кожної знахідки: файл, у чому ризик, як виправити. Якщо нічого критичного
не видно — прямо скажи це. Максимум 6 пунктів."""
    return await ai_service.generate_text(prompt, temperature=0.2)


async def optimize_suggestions(snapshot: dict) -> str | None:
    context = _format_context(snapshot)
    prompt = f"""Ти — AI-розробник, що пропонує покращення коду. Відповідай українською.

{context}

Запропонуй конкретні покращення: продуктивність, читабельність, дублювання коду,
застарілі підходи. Для кожного пункту — файл і коротке пояснення що і чому змінити.
Максимум 6 пунктів, без загальних фраз."""
    return await ai_service.generate_text(prompt, temperature=0.4)


async def ask_about_code(snapshot: dict, question: str) -> str | None:
    context = _format_context(snapshot)
    prompt = f"""Ти — AI-розробник, що відповідає на питання про конкретний репозиторій. Відповідай українською, конкретно і по суті.

{context}

Питання користувача:
"{question}"

Відповідай, спираючись ЛИШЕ на показаний код. Якщо для точної відповіді
бракує контексту (файл не показано вище) — чесно скажи це і назви, який
файл варто було б показати."""
    return await ai_service.generate_text(prompt, temperature=0.4)


async def plan_code_change(snapshot: dict, instruction: str) -> dict | None:
    """Головна фішка: AI аналізує репозиторій і повертає готовий план змін
    (які файли і з яким повним новим вмістом). Повертає None, якщо AI не
    зміг сформувати валідний план."""
    context = _format_context(snapshot)
    prompt = f"""Ти — AI-розробник, що вносить зміни в код репозиторію за описом користувача. Відповідай українською (крім самого коду).

{context}

Завдання від користувача:
"{instruction}"

Правила:
- Онови МІНІМАЛЬНУ необхідну кількість файлів (максимум 5).
- Для КОЖНОГО файлу, який змінюєш, поверни ПОВНИЙ новий вміст файлу
  (не фрагмент, не diff) — так, щоб цей вміст можна було напряму записати
  замість старого файлу.
- Якщо потрібен новий файл — постав action "create" і теж дай повний вміст.
- Не видаляй і не чіпай файли, яких завдання не стосується.
- Якщо для якісного виконання завдання бракує вмісту важливого файлу, якого
  немає у показаному контексті (він міг не потрапити через обмеження
  розміру) — все одно зроби найкращу можливу спробу і згадай це в "summary".

Поверни ВИКЛЮЧНО JSON без жодного тексту навколо, без markdown-розмітки:
{{
  "summary": "короткий опис українською, що саме буде зроблено і чому",
  "commit_message": "короткий commit message англійською, у стилі conventional commits",
  "files": [
    {{"path": "шлях/до/файлу.ext", "action": "modify", "new_content": "повний новий вміст файлу"}}
  ]
}}"""
    data = await ai_service.generate_json(prompt, temperature=0.25)
    if not data or not isinstance(data.get("files"), list) or not data["files"]:
        return None

    files_out = []
    for f in data["files"]:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip().lstrip("/")
        content = f.get("new_content")
        if not path or content is None:
            continue
        action = f.get("action") if f.get("action") in ("modify", "create") else "modify"
        files_out.append({"path": path, "action": action, "new_content": str(content)})
    if not files_out:
        return None

    return {
        "summary": str(data.get("summary", "")).strip()[:500] or "AI-зміна без опису.",
        "commit_message": str(data.get("commit_message", "")).strip()[:200] or "AI Developer update",
        "files": files_out[:5],
    }


async def apply_code_change(token: str, owner: str, repo: str, branch: str, plan: dict) -> str:
    """Застосовує план змін через github_api.deploy_files (той самий шлях,
    що й звичайний деплой ZIP). "До"-знімок для undo caller формує САМ із
    вже наявного snapshot["files_text"] (тут його не вигадуємо — інакше
    для action="modify" ми б помилково вважали файл новим)."""
    files_bytes = {f["path"]: f["new_content"].encode("utf-8") for f in plan["files"]}
    return await github_api.deploy_files(
        token, owner, repo, branch, files_bytes, plan["commit_message"],
    )


async def undo_change(
    token: str, owner: str, repo: str, branch: str, before_files: dict[str, str | None],
) -> tuple[str | None, list[str]]:
    """Відновлює попередній вміст файлів, які AI ЗМІНЮВАВ (є "до"-версія).
    Файли, які AI СТВОРИВ (before=None), НЕ видаляються автоматично — Git
    Trees API (яким тут усе працює, як і в deploy_files) не підтримує
    видалення без окремого небезпечного flow. Повертає (commit_sha_або_None,
    список_пропущених_нових_файлів)."""
    restorable = {p: c.encode("utf-8") for p, c in before_files.items() if c is not None}
    skipped_new = [p for p, c in before_files.items() if c is None]
    if not restorable:
        return None, skipped_new
    commit_sha = await github_api.deploy_files(
        token, owner, repo, branch, restorable, "Revert AI Developer change",
    )
    return commit_sha, skipped_new