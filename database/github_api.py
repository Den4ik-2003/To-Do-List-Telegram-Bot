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
            raise GithubDeployError(f"blob create failed: {data}")
        return data["sha"]


async def _create_tree(session, owner, repo, base_tree_sha, tree_items):
    payload = {"tree": tree_items}
    if base_tree_sha:
        payload["base_tree"] = base_tree_sha
    async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            raise GithubDeployError(f"tree create failed: {data}")
        return data["sha"]


async def _create_commit(session, owner, repo, message, tree_sha, parent_sha):
    payload = {"message": message, "tree": tree_sha}
    if parent_sha:
        payload["parents"] = [parent_sha]
    async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/commits", json=payload) as resp:
        data = await resp.json()
        if resp.status not in (200, 201):
            raise GithubDeployError(f"commit create failed: {data}")
        return data["sha"]


async def _update_ref(session, owner, repo, branch, commit_sha, create_branch=False):
    if create_branch:
        payload = {"ref": f"refs/heads/{branch}", "sha": commit_sha}
        async with session.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs", json=payload) as resp:
            if resp.status not in (200, 201):
                data = await resp.json()
                raise GithubDeployError(f"ref create failed: {data}")
    else:
        payload = {"sha": commit_sha, "force": False}
        async with session.patch(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}", json=payload) as resp:
            if resp.status not in (200, 201):
                data = await resp.json()
                raise GithubDeployError(f"ref update failed: {data}")


async def deploy_files(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    files: dict[str, bytes],
    commit_message: str,
    progress_cb=None,
) -> str:
    async with aiohttp.ClientSession(headers=_headers(token), timeout=aiohttp.ClientTimeout(total=90)) as session:
        parent_sha = await _get_branch_sha(session, owner, repo, branch)
        base_tree_sha = None
        if parent_sha:
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
        await _update_ref(session, owner, repo, branch, commit_sha, create_branch=parent_sha is None)
        return commit_sha