import asyncio
import json
import os
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import aiohttp

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)
POLL_INTERVAL = int(os.getenv("DEPLOY_POLL_INTERVAL", "60"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
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
        print(f"DEPLOY NOTIFIER STATE ERROR: {e}")


async def fetch_json(session, url, params=None):
    async with session.get(url, params=params) as response:
        if response.status != 200:
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


def build_message(event, details):
    repo = event["repo"]["name"]
    payload = event["payload"]
    branch = payload.get("ref", "").replace("refs/heads/", "")
    moment = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).astimezone(TIMEZONE)
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


async def tick(bot, session, state):
    events = await fetch_json(
        session, f"{API}/users/{GITHUB_USERNAME}/events", {"per_page": 100}
    )
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
            OWNER_CHAT_ID,
            build_message(event, details),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        state["last_id"] = int(event["id"])
        save_state(state)

    if ids and max(ids) > state["last_id"]:
        state["last_id"] = max(ids)
        save_state(state)


async def run(bot):
    if not (GITHUB_TOKEN and GITHUB_USERNAME and OWNER_CHAT_ID):
        print("DEPLOY NOTIFIER DISABLED: set GITHUB_TOKEN, GITHUB_USERNAME, OWNER_CHAT_ID")
        return

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    state = load_state()

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                await tick(bot, session, state)
            except Exception as e:
                print(f"DEPLOY NOTIFIER ERROR: {e}")
            await asyncio.sleep(POLL_INTERVAL)