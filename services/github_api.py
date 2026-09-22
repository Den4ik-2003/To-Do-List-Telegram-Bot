import base64
import logging
import re

import aiohttp

logger = logging.getLogger("tasks_bot")

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 20

_REPO_URL_RE = re.compile(r"github\.com[:/]+([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")


class GithubDeployError(Exception):
    pass


def parse_repo_url(url: str) -> tuple[str, str] | None:
    m = _REPO_URL_RE.search((url or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _friendly_error(status: int, data: dict, owner: str = "", repo: str = "") -> str:
    """
    Перетворює сиру відповідь GitHub API на зрозуміле користувачу пояснення.
    GitHub повертає однакові статус-коди (403/404/422) для дуже різних
    причин, тому розрізняємо їх за текстом message, а не лише за кодом.
    """
    message = (data or {}).get("message", "") if isinstance(data, dict) else str(data)
    repo_hint = f"{owner}/{repo}" if owner and repo else "цей репозиторій"

    if status == 403:
        if "Resource not accessible by personal access token" in message:
            return (
                f"Токен не має прав писати в {repo_hint}. Перевір:\n"
                f"1) Якщо це fine-grained token — чи вибрано саме цей репозиторій "
                f"у списку доступних, і чи увімкнено право «Contents: Read and write».\n"
                f"2) Якщо це classic token — чи є scope «repo» (для приватних) "
                f"або «public_repo» (для публічних).\n"
                f"3) Якщо репозиторій належить організації з SSO — чи авторизовано "
                f"токен під цю організацію (кнопка «Configure SSO» біля токена в GitHub Settings)."
            )
        if "rate limit" in message.lower():
            return "Вичерпано ліміт запитів до GitHub API. Спробуй ще раз за кілька хвилин."
        return f"GitHub відмовив у доступі (403): {message or 'невідома причина'}"

    if status == 404:
        return (
            f"Репозиторій {repo_hint} не знайдено, або токен не має доступу навіть на читання. "
            f"Перевір назву репозиторію та права токена."
        )

    if status == 422:
        return f"GitHub відхилив запит як некоректний (422): {message or 'невідома причина'}"

    return f"Помилка GitHub API ({status}): {message or data}"


async def verify_token(token: str) -> dict | None:
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/user", timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:
        logger.exception("GitHub verify_token failed")
        return None


async def list_repos(token: str, limit: int = 50) -> list[dict]:
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/user/repos",
                params={"per_page": str(limit), "sort": "updated", "affiliation": "owner,collaborator"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    {
                        "full_name": r["full_name"],
                        "owner": r["owner"]["login"],
                        "name": r["name"],
                        "default_branch": r.get("default_branch", "main"),
                        "private": r.get("private", False),
                    }
                    for r in data
                ]
    except Exception:
        logger.exception("GitHub list_repos failed")
        return []


async def get_repo(token: str, owner: str, repo: str) -> dict | None:
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return {
                    "full_name": data["full_name"],
                    "owner": data["owner"]["login"],
                    "name": data["name"],
                    "default_branch": data.get("default_branch", "main"),
                    "private": data.get("private", False),
                    "empty": data.get("size", 0) == 0,
                }
    except Exception:
        logger.exception("GitHub get_repo failed for %s/%s", owner, repo)
        return None


async def check_write_access(token: str, owner: str, repo: str) -> str | None:
    """
    Легка перевірка ПЕРЕД деплоєм: чи взагалі токен має право писати
    в репозиторій. GET /repos/{owner}/{repo} повертає поле "permissions"
    (push/admin/pull), яке видно навіть без спроби реального запису —
    це дозволяє показати зрозумілу помилку одразу, а не після того, як
    користувач уже чекав на прогрес деплою і впав на кроці ініціалізації.
    Повертає None, якщо все гаразд, або готовий текст помилки для показу.
    """
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    data = await resp.json()
                    return _friendly_error(resp.status, data, owner, repo)
                data = await resp.json()
                perms = data.get("permissions", {})
                if not perms.get("push", False):
                    return (
                        f"Токен має доступ лише на читання {owner}/{repo} — "
                        f"права на запис (push) немає. Перевір scope/permissions токена."
                    )
                return None
    except Exception:
        logger.exception("GitHub check_write_access failed for %s/%s", owner, repo)
        return "Не вдалося перевірити права доступу токена (мережева помилка). Спробуй ще раз."


async def list_branches(token: str, owner: str, repo: str) -> list[dict]:
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/branches",
                params={"per_page": "100"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [{"name": b["name"], "sha": b["commit"]["sha"]} for b in data]
    except Exception:
        logger.exception("GitHub list_branches failed for %s/%s", owner, repo)
        return []


async def list_commits(token: str, owner: str, repo: str, branch: str, limit: int = 10) -> list[dict]:
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/commits",
                params={"sha": branch, "per_page": str(limit)},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                out = []
                for c in data:
                    msg = (c.get("commit", {}).get("message") or "").split("\n", 1)[0]
                    out.append({
                        "sha": c["sha"],
                        "short_sha": c["sha"][:7],
                        "message": msg[:40] or "(без опису)",
                    })
                return out
    except Exception:
        logger.exception("GitHub list_commits failed for %s/%s", owner, repo)
        return []


async def get_tree_recursive(token: str, owner: str, repo: str, commit_sha: str) -> dict | None:
    """Resolves a commit's tree recursively. Returns only blob (file) entries,
    each with its GitHub blob sha and byte size — no content fetched yet."""
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/commits/{commit_sha}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                commit_data = await resp.json()
                tree_sha = commit_data["commit"]["tree"]["sha"]

            async with session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{tree_sha}",
                params={"recursive": "1"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = [
                    {"path": t["path"], "sha": t["sha"], "size": t.get("size", 0)}
                    for t in data.get("tree", [])
                    if t.get("type") == "blob"
                ]
                return {"tree_sha": tree_sha, "items": items, "truncated": data.get("truncated", False)}
    except Exception:
        logger.exception("GitHub get_tree_recursive failed for %s/%s@%s", owner, repo, commit_sha)
        return None


async def get_blob_content(session: aiohttp.ClientSession, owner: str, repo: str, sha: str) -> bytes | None:
    """Fetches one blob's content. Caller supplies an already-open, auth-headered session
    (see github_download.build_zip) so many blobs can be fetched without reconnecting."""
    async with session.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs/{sha}") as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        encoding = data.get("encoding")
        content = data.get("content", "")
        if encoding == "base64":
            return base64.b64decode(content)
        return content.encode("utf-8")


async def _get_branch_sha(session: aiohttp.ClientSession, owner: str, repo: str, branch: str) -> str | None:
    async with session.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}") as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data["object"]["sha"]


async def _get_commit_tree_sha(session: aiohttp.ClientSession, owner: str, repo: str, commit_sha: str) -> str:
    async with session.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/commits/{commit_sha}") as resp:
        data = await resp.json()
        return data["tree"]["sha"]


async def _create_blob(session: aiohttp.ClientSession, owner: str, repo: str, content: bytes) -> str:
    payload = {"content": base64.b64encode(content).decode(), "encoding": "base64"}
    async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))
        return data["sha"]


async def _create_tree(session, owner, repo, base_tree_sha, tree_items):
    payload = {"tree": tree_items}
    if base_tree_sha:
        payload["base_tree"] = base_tree_sha
    async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))
        return data["sha"]


async def _create_commit(session, owner, repo, message, tree_sha, parent_sha):
    payload = {"message": message, "tree": tree_sha}
    if parent_sha:
        payload["parents"] = [parent_sha]
    async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/commits", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))
        return data["sha"]


async def _update_ref(session, owner, repo, branch, commit_sha, create_branch=False):
    if create_branch:
        payload = {"ref": f"refs/heads/{branch}", "sha": commit_sha}
        async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs", json=payload) as resp:
            if resp.status not in (200, 201):
                data = await resp.json()
                raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))
    else:
        payload = {"sha": commit_sha, "force": False}
        async with session.patch(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}", json=payload) as resp:
            if resp.status not in (200, 201):
                data = await resp.json()
                raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))


async def _init_empty_repo(
    session: aiohttp.ClientSession, owner: str, repo: str, branch: str,
    seed_path: str, seed_content: bytes, message: str,
) -> str:
    """Creates the very first commit on a brand-new, completely empty
    repository, and the target branch along with it.

    The Git Data API (blobs/trees/commits) has no ref to attach to on an
    empty repo, so POST .../git/blobs fails with 'Git Repository is empty'
    no matter what. The Contents API doesn't have that restriction — it can
    create a single file from nothing, and GitHub creates the branch for it
    in the same call. This is the API equivalent of:

        echo "..." >> README.md
        git init
        git add README.md
        git commit -m "first commit"
        git branch -M main
        git remote add origin https://github.com/<owner>/<repo>.git
        git push -u origin main

    Returns the sha of the commit that was just created, so the caller can
    use it as the parent/base_tree for the rest of the files.
    """
    payload = {
        "message": message,
        "content": base64.b64encode(seed_content).decode(),
        "branch": branch,
    }
    async with session.put(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{seed_path}", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            # ВАЖЛИВО: раніше тут було "initial commit failed: {data}" — сирий
            # словник від GitHub, який нічого не пояснював користувачу
            # (саме це й вилізло в логах як голий traceback). Тепер повідомлення
            # одразу каже, яке саме право токена перевірити.
            raise GithubDeployError(_friendly_error(resp.status, data, owner, repo))
        return data["commit"]["sha"]


async def create_repo(token: str, name: str, private: bool = True) -> dict | None:
    async with aiohttp.ClientSession(headers=_headers(token)) as session:
        async with session.post(
            "https://api.github.com/user/repos",
            json={"name": name, "private": private, "auto_init": True},
        ) as resp:
            if resp.status not in (200, 201):
                return None
            data = await resp.json()
            return {
                "owner": data["owner"]["login"],
                "repo": data["name"],
                "default_branch": data.get("default_branch", "main"),
            }


async def repo_exists(token: str, owner: str, repo: str) -> bool:
    async with aiohttp.ClientSession(headers=_headers(token)) as session:
        async with session.get(f"https://api.github.com/repos/{owner}/{repo}") as resp:
            return resp.status == 200


async def delete_repo(token: str, owner: str, repo: str) -> bool:
    """Видаляє репозиторій НАЗАВЖДИ. Потребує, щоб токен мав scope
    delete_repo (для classic Personal Access Token) або дозвіл
    Administration: write (для fine-grained token) — без цього GitHub
    поверне 403, і функція коректно поверне False, не кидаючи виняток."""
    try:
        async with aiohttp.ClientSession(headers=_headers(token)) as session:
            async with session.delete(
                f"{GITHUB_API}/repos/{owner}/{repo}",
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 204:
                    return True
                if resp.status == 404:
                    logger.info("GitHub delete_repo: %s/%s вже не існує (404)", owner, repo)
                    return True
                body = await resp.text()
                logger.error("GitHub delete_repo failed for %s/%s: %s %s", owner, repo, resp.status, body[:300])
                return False
    except Exception:
        logger.exception("GitHub delete_repo crashed for %s/%s", owner, repo)
        return False


async def deploy_files(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    files: dict[str, bytes],
    commit_message: str,
    progress_cb=None,
) -> str:
    if not files:
        raise GithubDeployError("no files to deploy")

    async with aiohttp.ClientSession(headers=_headers(token), timeout=aiohttp.ClientTimeout(total=90)) as session:
        parent_sha = await _get_branch_sha(session, owner, repo, branch)

        if parent_sha is None:
            # Brand-new / completely empty repo (or the branch doesn't exist
            # yet): there is no commit for the Git Data API to build on top
            # of, so bootstrap it first via the Contents API — same result
            # as `git init && git commit && git push -u origin main`.
            seed_path, seed_content = next(iter(files.items()))
            logger.info(
                "Repo %s/%s branch %s has no commits yet — bootstrapping via contents API",
                owner, repo, branch,
            )
            try:
                parent_sha = await _init_empty_repo(
                    session, owner, repo, branch, seed_path, seed_content,
                    "first commit",
                )
            except GithubDeployError:
                # ДОДАНО: логуємо саме тут з повним контекстом (owner/repo/branch),
                # щоб при потребі шукати в логах Render було зрозуміло, який саме
                # репозиторій і крок впали, не гортаючи весь traceback вручну.
                logger.error(
                    "GitHub deploy: bootstrap порожнього репо %s/%s@%s не вдався",
                    owner, repo, branch,
                )
                raise

        base_tree_sha = await _get_commit_tree_sha(session, owner, repo, parent_sha)

        tree_items = []
        total = len(files)
        for i, (path, content) in enumerate(files.items(), start=1):
            blob_sha = await _create_blob(session, owner, repo, content)
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
            if progress_cb:
                await progress_cb(i, total)

        tree_sha = await _create_tree(session, owner, repo, base_tree_sha, tree_items)
        commit_sha = await _create_commit(session, owner, repo, commit_message, tree_sha, parent_sha)
        # The branch now always exists by this point — either it already did,
        # or _init_empty_repo just created it — so this is always an update.
        await _update_ref(session, owner, repo, branch, commit_sha, create_branch=False)
        return commit_sha