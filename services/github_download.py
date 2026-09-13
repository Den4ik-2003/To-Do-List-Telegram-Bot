import io
import logging
import zipfile

import aiohttp

from services import github_api

logger = logging.getLogger("tasks_bot")

# Bots uploading a file directly to Telegram (not via URL/file_id) are capped at 50 MB
# by the standard Bot API. This is a different, separate limit from the 20 MB cap on
# files a bot can *receive* — see github_zip.MAX_ZIP_SIZE for that one.
MAX_SEND_ZIP_SIZE = 50 * 1024 * 1024


class DownloadError(Exception):
    def __init__(self, reason: str, hint: str):
        self.reason = reason
        self.hint = hint
        super().__init__(reason)


def fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} КБ"
    return f"{num_bytes / (1024 * 1024):.1f} МБ"


async def prepare_download(token: str, owner: str, repo: str, commit_sha: str) -> dict:
    """Resolves the tree for a commit and checks its total size BEFORE fetching any
    file content, so an oversized repo fails fast with a clear message."""
    tree = await github_api.get_tree_recursive(token, owner, repo, commit_sha)
    if tree is None:
        raise DownloadError(
            "Не вдалося отримати вміст repository.",
            "Перевір права токена і спробуй ще раз.",
        )

    items = tree["items"]
    if not items:
        raise DownloadError(
            "Repository порожній.",
            "Немає файлів для завантаження.",
        )

    total_size = sum(i.get("size") or 0 for i in items)
    if total_size > MAX_SEND_ZIP_SIZE:
        raise DownloadError(
            f"Repository занадто великий для завантаження через Telegram "
            f"({fmt_size(total_size)}, ліміт — {MAX_SEND_ZIP_SIZE // (1024 * 1024)} МБ).",
            "Очисти repository (наприклад важкі build/node_modules файли в гілці) "
            "або клонуй його локально через Git.",
        )

    return {"items": items, "total_size": total_size, "truncated": tree["truncated"]}


async def build_zip(
    token: str,
    owner: str,
    repo: str,
    items: list[dict],
    root_folder: str,
    progress_cb=None,
) -> bytes:
    """Fetches every blob and packs it into a ZIP under root_folder/, preserving
    the repo's folder structure. No .git or GitHub-internal metadata is included —
    the tree API only ever returns tracked project files."""
    buf = io.BytesIO()
    total = len(items)

    async with aiohttp.ClientSession(
        headers=github_api._headers(token), timeout=aiohttp.ClientTimeout(total=120)
    ) as session:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, item in enumerate(items, start=1):
                try:
                    content = await github_api.get_blob_content(session, owner, repo, item["sha"])
                except Exception:
                    logger.exception("Failed to fetch blob %s (%s)", item["sha"], item["path"])
                    content = None
                if content is None:
                    raise DownloadError(
                        f"Не вдалося завантажити файл {item['path']} з GitHub.",
                        "Спробуй ще раз.",
                    )
                zf.writestr(f"{root_folder}/{item['path']}", content)
                if progress_cb:
                    await progress_cb(i, total)

    return buf.getvalue()