import asyncio
import json
import logging
import os
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger("deploy_notifier")

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy_notifier_state.json",
)
ZERO_SHA = "0" * 40
API = "https://api.github.com"


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_id": None}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("State save error: %s", e)


async def fetch_json(session, url, params=None):
    async with session.get(url, params=params) as response:
        if response.status != 200:
            body = await response.text()
            logger.error("GitHub %s -> %s %s", url, response.status, body[:200])
            return None
        return await response.json()


async def fetch_details(session, repo, before, head):
    if before and before != ZERO_SHA:
        data = await fetch_json(session, f"{API}/repos/{repo}/compare/{before}...{head}")
        if data:
            return {
                "commits": data.get("commits", []),
                "total": data.get("total_commits", len(data.get("commits", []))),
                "files": len(data.get("files", [])),
                "url": data.get("html_url", ""),
            }
    data = await fetch_json(session, f"{API}/repos/{repo}/commits/{head}")
    if data:
        return {
            "commits": [data],
            "total": 1,
            "files": len(data.get("files", [])),
            "url": data.get("html_url", ""),
        }
    return {"commits": [], "total": 0, "files": 0, "url": f"https://github.com/{repo}/commit/{head}"}


def build_message(event, details, tz):
    repo = event["repo"]["name"]
    payload = event["payload"]
    branch = payload.get("ref", "").replace("refs/heads/", "")
    moment = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).astimezone(tz)
    head = payload.get("head", "")

    lines = [
        "🚀 <b>Новий деплой на GitHub</b>",
        "",
        f"📦 <b>Репозиторій:</b> <a href=\"https://github.com/{repo}\">{escape(repo)}</a>",
        f"🌿 <b>Гілка:</b> {escape(branch)}",
        f"🕒 <b>Дата:</b> {moment.strftime('%d.%m.%Y %H:%M:%S')}",
        f"🔑 <b>Коміт:</b> <code>{escape(head[:7])}</code>",
        f"📝 <b>Комітів:</b> {details['total']}",
    ]
    if details["files"]:
        lines.append(f"📄 <b>Змінено файлів:</b> {details['files']}")

    commits = details["commits"]
    if commits:
        lines.append("")
        lines.append("<b>Коміти:</b>")
        for commit in commits[-5:]:
            info = commit.get("commit", {})
            message = (info.get("message", "") or "").split("\n")[0][:80]
            author = (info.get("author", {}) or {}).get("name", "")
            lines.append(f"• {escape(message)} — <i>{escape(author)}</i>")
        if len(commits) > 5:
            lines.append(f"… і ще {len(commits) - 5}")

    if details["url"]:
        lines.append("")
        lines.append(f"🔗 <a href=\"{escape(details['url'])}\">Переглянути зміни</a>")

    return "\n".join(lines)


async def tick(bot, session, state, username, chat_id, tz):
    events = await fetch_json(session, f"{API}/users/{username}/events", {"per_page": 100})
    if events is None:
        return

    ids = [int(e["id"]) for e in events]

    if state.get("last_id") is None:
        state["last_id"] = max(ids) if ids else 0
        save_state(state)
        return

    last_id = state["last_id"]
    fresh = sorted(
        (e for e in events if e["type"] == "PushEvent" and int(e["id"]) > last_id),
        key=lambda e: int(e["id"]),
    )

    for event in fresh:
        payload = event["payload"]
        head = payload.get("head", "")
        if not head or head == ZERO_SHA:
            state["last_id"] = int(event["id"])
            save_state(state)
            continue
        details = await fetch_details(
            session, event["repo"]["name"], payload.get("before", ""), head
        )
        await bot.send_message(
            chat_id,
            build_message(event, details, tz),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        state["last_id"] = int(event["id"])
        save_state(state)

    if ids and max(ids) > state["last_id"]:
        state["last_id"] = max(ids)
        save_state(state)


async def run(bot):
    token = os.getenv("GITHUB_TOKEN", "").strip()
    username = os.getenv("GITHUB_USERNAME", "").strip()
    chat_raw = os.getenv("OWNER_CHAT_ID", "").strip()
    interval = int(os.getenv("DEPLOY_POLL_INTERVAL", "60") or 60)
    tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))

    missing = [
        name
        for name, value in (
            ("GITHUB_TOKEN", token),
            ("GITHUB_USERNAME", username),
            ("OWNER_CHAT_ID", chat_raw),
        )
        if not value
    ]
    if missing:
        logger.error("Deploy notifier disabled, missing env: %s", ", ".join(missing))
        return

    try:
        chat_id = int(chat_raw)
    except ValueError:
        logger.error("OWNER_CHAT_ID must be a number, got: %s", chat_raw)
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    state = load_state()

    try:
        await bot.send_message(
            chat_id,
            f"✅ Деплой-нотифікатор запущено для <b>{escape(username)}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Cannot send startup message to %s: %s", chat_id, e)

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                await tick(bot, session, state, username, chat_id, tz)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Deploy notifier error: %s", e)
            await asyncio.sleep(interval)