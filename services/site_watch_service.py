import logging

import aiohttp

logger = logging.getLogger("tasks_bot")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 SiteWatchBot"
    ),
}


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


async def check_site(url: str, timeout_seconds: int = 10) -> bool:
    """True — сайт доступний (2xx-3xx), False — недоступний/помилка/таймаут."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=True,
                ) as resp:
                    return resp.status < 500
            except aiohttp.ClientError:
                # Деякі сервери банять HEAD/GET без певних заголовків або не
                # підтримують якийсь метод — пробуємо HEAD як запасний варіант.
                async with session.head(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=True,
                ) as resp:
                    return resp.status < 500
    except Exception:
        logger.warning("Site check failed for %s", url)
        return False