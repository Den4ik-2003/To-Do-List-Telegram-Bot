import asyncio
import json
import logging
import os
import time
from html import escape

import aiohttp

logger = logging.getLogger("deploy_notifier")

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy_notifier_state.json",
)
# Стан зберігаємо в MongoDB (user_state власника), бо файлова система Render
# стирається при кожному деплої, а саме на деплої бот і перезапускається.
STATE_KEY = "deploy_notifier_state"

ZERO_SHA = "0" * 40
API = "https://api.github.com"

AUTH_RETRY_SECONDS = 600
MAX_SEND_FAILS = 3
SEEN_LIMIT = 2000


class GitHubAuthError(Exception):
    """GitHub відхилив токен (401)."""


class GitHubRateLimit(Exception):
    """Вичерпано ліміт запитів GitHub."""

    def __init__(self, reset_in: int):
        super().__init__(f"rate limited, reset in {reset_in}s")
        self.reset_in = reset_in


# ───────────────────────────── стан ─────────────────────────────

def _empty_state() -> dict:
    return {"last_ids": {}}


def _read_file_state() -> dict | None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if isinstance(data.get("last_ids"), dict):
        return data
    if data.get("last_id") is not None:  # старий формат
        return {"last_ids": {"user": int(data["last_id"])}}
    return None


async def load_state(chat_id: int) -> dict:
    try:
        from database.users import get_user_state
        data = await get_user_state(chat_id) or {}
        state = data.get(STATE_KEY)
        if isinstance(state, dict) and isinstance(state.get("last_ids"), dict):
            return state
    except Exception as e:
        logger.warning("Не вдалося прочитати стан з MongoDB: %s", e)
    return _read_file_state() or _empty_state()


async def save_state(chat_id: int, state: dict) -> None:
    try:
        from database.users import save_user_state
        await save_user_state(chat_id, {STATE_KEY: state})
    except Exception as e:
        logger.error("Не вдалося зберегти стан у MongoDB: %s", e)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("State file save error: %s", e)


# ───────────────────────────── GitHub API ─────────────────────────────

async def _request(session, url, params=None, etag=None):
    """Повертає (status, data, etag, poll_interval)."""
    headers = {"If-None-Match": etag} if etag else None
    async with session.get(url, params=params, headers=headers) as response:
        status = response.status
        poll = int(response.headers.get("X-Poll-Interval", 0) or 0)
        if status == 200:
            return status, await response.json(), response.headers.get("ETag"), poll
        if status == 304:
            return status, None, etag, poll
        body = await response.text()
        if status == 401:
            raise GitHubAuthError(body[:200])
        if status == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(response.headers.get("X-RateLimit-Reset", 0) or 0)
            raise GitHubRateLimit(max(reset - int(time.time()), 60))
        logger.error("GitHub %s -> %s %s", url, status, body[:200])
        return status, None, None, poll


async def fetch_json(session, url, params=None):
    _, data, _, _ = await _request(session, url, params)
    return data


async def fetch_details(session, repo, before, head):
    if before and before != ZERO_SHA:
        data = await fetch_json(session, f"{API}/repos/{repo}/compare/{before}...{head}")
        if data:
            return {
                "commits": data.get("commits", []),
                "files": len(data.get("files", [])),
                "url": data.get("html_url", ""),
            }
    data = await fetch_json(session, f"{API}/repos/{repo}/commits/{head}")
    if data:
        return {
            "commits": [data],
            "files": len(data.get("files", [])),
            "url": data.get("html_url", ""),
        }
    return {"commits": [], "files": 0, "url": f"https://github.com/{repo}/commit/{head}"}


# ───────────────────────────── повідомлення ─────────────────────────────

def pick_author(event, details):
    commits = details["commits"]
    if commits:
        last = commits[-1]
        name = ((last.get("commit", {}) or {}).get("author", {}) or {}).get("name", "")
        if name:
            return name
    return (event.get("actor", {}) or {}).get("login", "") or "—"


def last_commit_title(details):
    commits = details["commits"]
    if not commits:
        return ""
    message = ((commits[-1].get("commit", {}) or {}).get("message", "") or "").split("\n")[0]
    return message[:80]


def _branch(payload) -> str:
    ref = payload.get("ref", "") or ""
    return ref.removeprefix("refs/heads/")


def build_message(event, details):
    repo = event["repo"]["name"]
    payload = event["payload"]
    is_new = payload.get("before", "") == ZERO_SHA
    author = pick_author(event, details)
    branch = _branch(payload)
    repo_link = f"<a href=\"https://github.com/{repo}\">{escape(repo)}</a>"

    lines = [
        "🚀 <b>Новий деплой</b>" if is_new else "🛠 <b>Доробка</b>",
        "",
        f"📦 репо: {repo_link}",
        f"👤 хто: <b>{escape(author)}</b>",
    ]
    if branch:
        lines.append(f"🌿 гілка: {escape(branch)}")
    if is_new:
        return "\n".join(lines)

    if details["files"]:
        lines.append(f"📄 файлів: {details['files']} оновлено")
    title = last_commit_title(details)
    if title:
        lines.append(f"📝 {escape(title)}")
    if details["url"]:
        lines.append(f"🔗 <a href=\"{escape(details['url'])}\">зміни</a>")
    return "\n".join(lines)


def build_repo_created_message(event):
    repo = event["repo"]["name"]
    actor = (event.get("actor", {}) or {}).get("login", "") or "—"
    repo_link = f"<a href=\"https://github.com/{repo}\">{escape(repo)}</a>"
    return "\n".join([
        "🆕 <b>Новий репозиторій</b>",
        "",
        f"📦 репо: {repo_link}",
        f"👤 хто: <b>{escape(actor)}</b>",
    ])


def build_startup_message() -> str:
    service = os.getenv("RENDER_SERVICE_NAME", "").strip()
    branch = os.getenv("RENDER_GIT_BRANCH", "").strip()
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()[:7]
    lines = ["🟢 <b>Бот запущено</b> (деплой завершено)", ""]
    if service:
        lines.append(f"🖥 сервіс: <b>{escape(service)}</b>")
    if branch:
        lines.append(f"🌿 гілка: {escape(branch)}")
    if commit:
        lines.append(f"🔖 коміт: <code>{escape(commit)}</code>")
    return "\n".join(lines)


async def _send(bot, chat_id, text) -> None:
    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _safe_notify(bot, chat_id, text) -> None:
    try:
        await _send(bot, chat_id, text)
    except Exception as e:
        logger.error("Не вдалося надіслати службове повідомлення: %s", e)


# ───────────────────────────── опитування ─────────────────────────────

async def poll_source(bot, session, state, ctx, source, url, chat_id) -> int:
    """Обробляє одну стрічку подій. Повертає X-Poll-Interval від GitHub."""
    status, events, etag, poll = await _request(
        session, url, {"per_page": 100}, ctx["etags"].get(source)
    )
    if status == 304 or events is None:
        return poll
    ctx["etags"][source] = etag

    ids = [int(e["id"]) for e in events]
    last_ids = state["last_ids"]

    if source not in last_ids:
        # Перший запуск за весь час: запам'ятовуємо точку відліку, без спаму старими подіями.
        last_ids[source] = max(ids) if ids else 0
        await save_state(chat_id, state)
        logger.info("Deploy notifier: джерело %s ініціалізовано (last_id=%s)", source, last_ids[source])
        return poll

    last_id = last_ids[source]
    fresh = sorted(
        (
            e for e in events
            if int(e["id"]) > last_id
            and (e["type"] == "PushEvent"
                 or (e["type"] == "CreateEvent" and (e.get("payload") or {}).get("ref_type") == "repository"))
        ),
        key=lambda e: int(e["id"]),
    )

    for event in fresh:
        eid = int(event["id"])

        if eid in ctx["seen"]:  # та сама подія могла прийти з іншої стрічки (користувач + організація)
            last_ids[source] = eid
            continue

        try:
            if event["type"] == "CreateEvent":
                text = build_repo_created_message(event)
            else:
                payload = event["payload"]
                head = payload.get("head", "")
                if not head or head == ZERO_SHA:
                    last_ids[source] = eid
                    await save_state(chat_id, state)
                    continue
                details = await fetch_details(
                    session, event["repo"]["name"], payload.get("before", ""), head
                )
                text = build_message(event, details)
            await _send(bot, chat_id, text)
        except (GitHubAuthError, GitHubRateLimit, asyncio.CancelledError):
            raise
        except Exception as e:
            ctx["fails"][eid] = ctx["fails"].get(eid, 0) + 1
            logger.error(
                "Не вдалося надіслати сповіщення про подію %s (спроба %d/%d): %s",
                eid, ctx["fails"][eid], MAX_SEND_FAILS, e,
            )
            if ctx["fails"][eid] < MAX_SEND_FAILS:
                return poll  # не просуваємо last_id: повторимо на наступному циклі
            # після кількох невдач пропускаємо подію, щоб одна проблемна не блокувала решту

        ctx["seen"].add(eid)
        if len(ctx["seen"]) > SEEN_LIMIT:
            ctx["seen"] = set(sorted(ctx["seen"])[SEEN_LIMIT // 2:])
        last_ids[source] = eid
        await save_state(chat_id, state)

    if ids and max(ids) > last_ids[source]:
        last_ids[source] = max(ids)
        await save_state(chat_id, state)
    return poll


async def tick(bot, session, state, ctx, username, orgs, chat_id) -> int:
    sources = [("user", f"{API}/users/{username}/events")]
    for org in orgs:
        sources.append((f"org_{org}", f"{API}/users/{username}/events/orgs/{org}"))

    poll = 0
    for source, url in sources:
        poll = max(poll, await poll_source(bot, session, state, ctx, source, url, chat_id))
    return poll


async def run(bot):
    token = os.getenv("GITHUB_TOKEN", "").strip().strip("\"'")
    username = os.getenv("GITHUB_USERNAME", "").strip()
    chat_raw = os.getenv("OWNER_CHAT_ID", "").strip()
    orgs = [o.strip() for o in os.getenv("GITHUB_ORGS", "").split(",") if o.strip()]
    notice_on = os.getenv("DEPLOY_STARTUP_NOTICE", "1").strip().lower() not in ("0", "false", "no", "off")
    try:
        interval = max(int(os.getenv("DEPLOY_POLL_INTERVAL", "60") or 60), 30)
    except ValueError:
        interval = 60

    try:
        chat_id = int(chat_raw)
    except ValueError:
        logger.error("OWNER_CHAT_ID must be a number, got: %r", chat_raw)
        return

    # Це справжній сигнал «деплой бота завершено»: бот щойно стартував на новій версії.
    if notice_on:
        await _safe_notify(bot, chat_id, build_startup_message())

    missing = [
        name
        for name, value in (("GITHUB_TOKEN", token), ("GITHUB_USERNAME", username))
        if not value
    ]
    if missing:
        logger.error("Deploy notifier disabled, missing env: %s", ", ".join(missing))
        await _safe_notify(
            bot, chat_id,
            f"⚠️ Сповіщення про деплої на GitHub вимкнено: не задано {escape(', '.join(missing))}.",
        )
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    state = await load_state(chat_id)
    ctx = {"etags": {}, "seen": set(), "fails": {}}
    auth_alerted = False

    logger.info(
        "Deploy notifier запущено: user=%s, orgs=%s, інтервал=%ds",
        username, orgs or "—", interval,
    )

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            sleep_for = interval
            try:
                poll = await tick(bot, session, state, ctx, username, orgs, chat_id)
                sleep_for = max(interval, poll)
                if auth_alerted:
                    auth_alerted = False
                    await _safe_notify(bot, chat_id, "✅ GitHub-токен знову працює, сповіщення відновлено.")
            except asyncio.CancelledError:
                raise
            except GitHubAuthError as e:
                logger.error("GitHub 401 Bad credentials: %s", e)
                if not auth_alerted:
                    auth_alerted = True
                    await _safe_notify(
                        bot, chat_id,
                        "⚠️ <b>GitHub-токен недійсний (401)</b>\n\n"
                        "Сповіщення про деплої не працюють. Створіть новий токен "
                        "(classic, scope <code>repo</code>) і оновіть <code>GITHUB_TOKEN</code> "
                        "у Render → Environment. Перевірятиму кожні 10 хв.",
                    )
                sleep_for = AUTH_RETRY_SECONDS
            except GitHubRateLimit as e:
                logger.warning("GitHub rate limit, чекаю %ds", e.reset_in)
                sleep_for = e.reset_in
            except Exception as e:
                logger.error("Deploy notifier error: %s", e)
            await asyncio.sleep(sleep_for)