"""
ЗМІНЕНИЙ ФАЙЛ: services/netlify_service.py

Єдина зміна відносно попередньої версії: _build_zip() тепер приймає
dict[str, str | bytes] замість dict[str, str] — бінарний контент (фото
товару, services/product_asset_service.py) пишеться в архів як є, без
utf-8 кодування; рядковий контент (html/css/js) кодується ЯК І РАНІШЕ.
Усі існуючі виклики deploy_new_site/redeploy_site з чистими текстовими
файлами поводяться ІДЕНТИЧНО попередній версії — нічого не зламано.
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


def _build_zip(files: dict[str, "str | bytes"]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            zf.writestr(path, data)
    return buf.getvalue()


def _extract_site_info(data: dict) -> dict:
    return {
        "site_id": data.get("id") or data.get("site_id"),
        "url": data.get("ssl_url") or data.get("url"),
        "admin_url": data.get("admin_url"),
        "name": data.get("name"),
    }


async def deploy_new_site(token: str, files: dict[str, "str | bytes"], desired_name: str | None = None) -> dict | None:
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
                logger.info("Netlify rename to %r failed, keeping auto-generated domain", desired_name)

        return info


async def redeploy_site(token: str, site_id: str, files: dict[str, "str | bytes"]) -> dict | None:
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