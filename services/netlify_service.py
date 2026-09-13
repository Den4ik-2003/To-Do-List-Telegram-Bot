"""
НОВИЙ ФАЙЛ: services/netlify_service.py

Тонка обгортка над Netlify REST API. Жодних нових залежностей — zip
збирається стандартним zipfile, HTTP через уже наявний aiohttp.

Використовується ОДИН спільний Netlify-акаунт (токен у налаштуваннях
сервера, NETLIFY_TOKEN) — так вирішили свідомо, а не "кожен юзер свій
токен", щоб не ускладнювати onboarding для фічі Website Builder.

Дві дії:
- deploy_new_site(...)  — створює НОВИЙ сайт на Netlify і одразу деплоїть
  у нього файли (Netlify підтримує "create+deploy" одним запитом: POST
  /sites з тілом = zip-архів).
- redeploy_site(...)    — деплоїть оновлені файли в УЖЕ існуючий сайт
  (POST /sites/{site_id}/deploys), URL сайту не змінюється.
"""

import io
import logging
import zipfile

import aiohttp

logger = logging.getLogger("tasks_bot")

API_BASE = "https://api.netlify.com/api/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _build_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content.encode("utf-8"))
    return buf.getvalue()


def _extract_site_info(data: dict) -> dict:
    return {
        "site_id": data.get("id") or data.get("site_id"),
        "url": data.get("ssl_url") or data.get("url"),
        "admin_url": data.get("admin_url"),
        "name": data.get("name"),
    }


async def deploy_new_site(token: str, files: dict[str, str], desired_name: str | None = None) -> dict | None:
    """Створює новий сайт на Netlify і одразу деплоїть у нього files.
    desired_name — бажаний піддомен (best-effort, якщо зайнятий — Netlify
    сам дасть випадковий, це не помилка)."""
    zip_bytes = _build_zip(files)
    async with aiohttp.ClientSession(headers=_headers(token), timeout=REQUEST_TIMEOUT) as session:
        async with session.post(
            f"{API_BASE}/sites",
            data=zip_bytes,
            headers={"Content-Type": "application/zip"},
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.error("Netlify create+deploy failed: %s %s", resp.status, body[:500])
                return None
            data = await resp.json()

        info = _extract_site_info(data)
        if not info["site_id"]:
            return None

        if desired_name:
            try:
                async with session.patch(
                    f"{API_BASE}/sites/{info['site_id']}",
                    json={"name": desired_name},
                ) as rename_resp:
                    if rename_resp.status in (200, 201):
                        renamed = await rename_resp.json()
                        info = _extract_site_info(renamed)
            except Exception:
                # Ім'я зайняте або інша дрібна помилка — не критично,
                # сайт уже живий на автоматично згенерованому домені.
                logger.info("Netlify rename to %r failed, keeping auto-generated domain", desired_name)

        return info


async def redeploy_site(token: str, site_id: str, files: dict[str, str]) -> dict | None:
    """Деплоїть новий вміст у вже існуючий сайт (той самий URL)."""
    zip_bytes = _build_zip(files)
    async with aiohttp.ClientSession(headers=_headers(token), timeout=REQUEST_TIMEOUT) as session:
        async with session.post(
            f"{API_BASE}/sites/{site_id}/deploys",
            data=zip_bytes,
            headers={"Content-Type": "application/zip"},
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.error("Netlify redeploy failed: %s %s", resp.status, body[:500])
                return None
            await resp.json()

        async with session.get(f"{API_BASE}/sites/{site_id}") as site_resp:
            if site_resp.status != 200:
                return {"site_id": site_id, "url": None, "admin_url": None, "name": None}
            site_data = await site_resp.json()
            return _extract_site_info(site_data)