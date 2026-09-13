"""
ЗМІНЕНИЙ ФАЙЛ: database/github_projects.py

Додано (для фічі "👨‍💻 AI Developer"):
- create_project(): новий проєкт тепер одразу має порожній "aiChangeHistory": [].
- record_ai_change(): пише метадані про застосовану AI-зміну (без вмісту
  файлів — тільки шляхи, summary, commit) у ТОЙ САМИЙ документ проєкту,
  тим самим патерном $push/$slice, що вже є для deployHistory/downloadHistory.
  Ніякої нової колекції — щоб нічого не дублювати.
- get_ai_change_history(): дістати історію AI-змін проєкту.

ВАЖЛИВО: старі проєкти (створені до цієї зміни) НЕ мають поля
aiChangeHistory в документі. Це не проблема — $push у MongoDB сам створює
масив, якщо його не було. get_ai_change_history() так само повертає [],
якщо поля нема. Міграція не потрібна.

Решта функцій файлу — 1:1 як було.
"""

import logging
from datetime import datetime

from bson import ObjectId

from database.mongo import github_projects_col, github_credentials_col, db_call

logger = logging.getLogger("tasks_bot")

MAX_HISTORY_ENTRIES = 20


async def save_credential(uid: int, encrypted_token: str, github_login: str) -> None:
    await db_call(
        github_credentials_col.update_one(
            {"userId": uid},
            {"$set": {
                "userId": uid,
                "encryptedToken": encrypted_token,
                "githubLogin": github_login,
                "connectedAt": datetime.utcnow().isoformat(),
            }},
            upsert=True,
        ),
        raise_on_fail=False,
    )


async def get_credential(uid: int) -> dict | None:
    return await db_call(github_credentials_col.find_one({"userId": uid}), raise_on_fail=False)


async def delete_credential(uid: int) -> bool:
    result = await db_call(github_credentials_col.delete_one({"userId": uid}), raise_on_fail=False)
    return bool(result and result.deleted_count)


async def list_projects(uid: int) -> list[dict]:
    cursor = github_projects_col.find({"userId": uid}).sort("updatedAt", -1)
    return await db_call(cursor.to_list(length=200), default=[], raise_on_fail=False)


async def get_project(uid: int, project_id) -> dict | None:
    try:
        oid = ObjectId(project_id)
    except Exception:
        return None
    return await db_call(github_projects_col.find_one({"_id": oid, "userId": uid}), raise_on_fail=False)


async def find_by_repo(uid: int, owner: str, repo: str) -> dict | None:
    return await db_call(
        github_projects_col.find_one({"userId": uid, "githubOwner": owner, "githubRepo": repo}),
        raise_on_fail=False,
    )


async def create_project(uid: int, name: str, owner: str, repo: str, default_branch: str, github_url: str) -> dict:
    now = datetime.utcnow().isoformat()
    doc = {
        "userId": uid,
        "projectName": name,
        "githubOwner": owner,
        "githubRepo": repo,
        "githubUrl": github_url,
        "defaultBranch": default_branch,
        "createdAt": now,
        "updatedAt": now,
        "lastDeployAt": None,
        "lastCommit": None,
        "deployHistory": [],
        "downloadHistory": [],
        "aiChangeHistory": [],
    }
    result = await db_call(github_projects_col.insert_one(doc), raise_on_fail=False)
    if result:
        doc["_id"] = result.inserted_id
    return doc


async def update_project(uid: int, project_id, updates: dict) -> bool:
    try:
        oid = ObjectId(project_id)
    except Exception:
        return False
    payload = dict(updates)
    payload["updatedAt"] = datetime.utcnow().isoformat()
    result = await db_call(
        github_projects_col.update_one({"_id": oid, "userId": uid}, {"$set": payload}),
        raise_on_fail=False,
    )
    return bool(result and result.matched_count)


async def delete_project(uid: int, project_id) -> bool:
    try:
        oid = ObjectId(project_id)
    except Exception:
        return False
    result = await db_call(github_projects_col.delete_one({"_id": oid, "userId": uid}), raise_on_fail=False)
    return bool(result and result.deleted_count)


async def record_deploy(uid: int, project_id, commit_sha: str, commit_message: str, file_count: int) -> None:
    try:
        oid = ObjectId(project_id)
    except Exception:
        return
    now = datetime.utcnow().isoformat()
    entry = {
        "at": now,
        "commit": commit_message,
        "commitSha": commit_sha,
        "fileCount": file_count,
    }
    await db_call(
        github_projects_col.update_one(
            {"_id": oid, "userId": uid},
            {
                "$set": {"lastDeployAt": now, "lastCommit": commit_message, "updatedAt": now},
                "$push": {"deployHistory": {"$each": [entry], "$position": 0, "$slice": MAX_HISTORY_ENTRIES}},
            },
        ),
        raise_on_fail=False,
    )


async def record_download(
    uid: int,
    project_id,
    branch: str,
    commit_sha: str,
    commit_message: str,
    file_count: int,
    archive_size: int,
) -> None:
    try:
        oid = ObjectId(project_id)
    except Exception:
        return
    now = datetime.utcnow().isoformat()
    entry = {
        "at": now,
        "branch": branch,
        "commitSha": commit_sha,
        "commit": commit_message,
        "fileCount": file_count,
        "archiveSize": archive_size,
    }
    await db_call(
        github_projects_col.update_one(
            {"_id": oid, "userId": uid},
            {
                "$set": {"updatedAt": now},
                "$push": {"downloadHistory": {"$each": [entry], "$position": 0, "$slice": MAX_HISTORY_ENTRIES}},
            },
        ),
        raise_on_fail=False,
    )


# =========================================================
# НОВЕ: 👨‍💻 AI Developer — історія AI-змін
# =========================================================

async def record_ai_change(
    uid: int,
    project_id,
    change_type: str,
    summary: str,
    commit_sha: str,
    commit_message: str,
    files_changed: list[str],
) -> None:
    """Записує метадані про застосовану AI-зміну в той самий документ
    проєкту (поле aiChangeHistory), тим самим $push/$slice патерном, що й
    record_deploy/record_download. Вміст файлів НЕ зберігається тут —
    тільки шляхи, щоб не роздувати документ; повний "до"-знімок для undo
    живе окремо в пам'яті процесу (handlers/ai_developer.py)."""
    try:
        oid = ObjectId(project_id)
    except Exception:
        return
    now = datetime.utcnow().isoformat()
    entry = {
        "at": now,
        "type": change_type,
        "summary": summary[:500],
        "commitSha": commit_sha,
        "commit": commit_message,
        "filesChanged": files_changed[:20],
    }
    await db_call(
        github_projects_col.update_one(
            {"_id": oid, "userId": uid},
            {
                "$set": {"updatedAt": now},
                "$push": {"aiChangeHistory": {"$each": [entry], "$position": 0, "$slice": MAX_HISTORY_ENTRIES}},
            },
        ),
        raise_on_fail=False,
    )


async def get_ai_change_history(uid: int, project_id) -> list[dict]:
    project = await get_project(uid, project_id)
    if not project:
        return []
    return project.get("aiChangeHistory") or []