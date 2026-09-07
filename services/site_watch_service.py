import hashlib
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from services import ai_service

logger = logging.getLogger("tasks_bot")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 SiteWatchBot"
    ),
}

SLOW_PAGE_MS = 2000
MAX_LINKS_TO_CHECK = 20


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
                async with session.head(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=True,
                ) as resp:
                    return resp.status < 500
    except Exception:
        logger.warning("Site check failed for %s", url)
        return False


def _same_domain(base_url: str, link: str) -> bool:
    try:
        return urlparse(base_url).netloc == urlparse(link).netloc
    except Exception:
        return False


async def _fetch(session: aiohttp.ClientSession, url: str, timeout: int = 12):
    start = time.monotonic()
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
        ) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            html = await resp.text(errors="ignore")
            return resp.status, elapsed_ms, html
    except Exception:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return None, elapsed_ms, ""


async def _head_or_get_status(session: aiohttp.ClientSession, url: str, timeout: int = 8) -> int | None:
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            if resp.status == 405:
                raise aiohttp.ClientError("HEAD not allowed")
            return resp.status
    except Exception:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
                return resp.status
        except Exception:
            return None


def _discover_internal_links(base_url: str, html: str, limit: int = 10) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(base_url, href)
        if not _same_domain(base_url, full_url):
            continue
        full_url = full_url.split("#")[0]
        if full_url in seen or full_url == base_url:
            continue
        seen.add(full_url)
        links.append(full_url)
        if len(links) >= limit:
            break
    return links


def _analyze_forms(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    total = len(forms)
    issues = []
    for i, form in enumerate(forms, start=1):
        action = form.get("action", "").strip()
        inputs = form.find_all(["input", "textarea", "select"])
        submit_btn = form.find(["button"]) or form.find("input", {"type": "submit"})

        if not action:
            issues.append(f"Форма #{i}: немає атрибута action (може не відправлятись)")
        if not submit_btn:
            issues.append(f"Форма #{i}: не знайдено кнопки відправки")
        named_inputs = [inp for inp in inputs if inp.get("name")]
        if inputs and not named_inputs:
            issues.append(f"Форма #{i}: жодне поле не має атрибута name (дані не дійдуть до сервера)")

    return {"total_forms": total, "issues": issues}


def _find_broken_images(base_url: str, html: str, limit: int = 10) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    srcs = []
    seen = set()
    for img in soup.find_all("img", src=True):
        src = urljoin(base_url, img["src"].strip())
        if src not in seen:
            seen.add(src)
            srcs.append(src)
        if len(srcs) >= limit:
            break
    return srcs


async def run_qa_scan(base_url: str, max_pages: int = 8) -> dict:
    report = {
        "base_url": base_url,
        "critical_error": None,
        "pages_checked": [],
        "broken_pages": [],
        "slow_pages": [],
        "forms_total": 0,
        "form_issues": [],
        "broken_images": [],
        "avg_response_ms": None,
    }

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        status, elapsed_ms, html = await _fetch(session, base_url)

        if status is None or status >= 500:
            report["critical_error"] = f"Головна сторінка не відповідає (статус: {status or 'timeout'})"
            return report

        report["pages_checked"].append({"url": base_url, "status": status, "ms": elapsed_ms})
        if status >= 400:
            report["broken_pages"].append({"url": base_url, "status": status})
        if elapsed_ms > SLOW_PAGE_MS:
            report["slow_pages"].append({"url": base_url, "ms": elapsed_ms})

        forms_data = _analyze_forms(html)
        report["forms_total"] = forms_data["total_forms"]
        report["form_issues"] = forms_data["issues"]

        internal_links = _discover_internal_links(base_url, html, limit=max_pages - 1)
        for link in internal_links:
            p_status, p_elapsed, p_html = await _fetch(session, link, timeout=10)
            entry = {"url": link, "status": p_status, "ms": p_elapsed}
            report["pages_checked"].append(entry)
            if p_status is None or p_status >= 400:
                report["broken_pages"].append({"url": link, "status": p_status or "timeout"})
            if p_elapsed > SLOW_PAGE_MS:
                report["slow_pages"].append({"url": link, "ms": p_elapsed})
            if p_html:
                sub_forms = _analyze_forms(p_html)
                report["forms_total"] += sub_forms["total_forms"]
                report["form_issues"].extend(f"[{link}] {issue}" for issue in sub_forms["issues"])

        broken_images = []
        image_candidates = _find_broken_images(base_url, html, limit=MAX_LINKS_TO_CHECK)
        for img_url in image_candidates:
            img_status = await _head_or_get_status(session, img_url)
            if img_status is None or img_status >= 400:
                broken_images.append(img_url)
        report["broken_images"] = broken_images

        times = [p["ms"] for p in report["pages_checked"] if p.get("ms") is not None]
        if times:
            report["avg_response_ms"] = int(sum(times) / len(times))

    return report


def format_qa_report(report: dict) -> str:
    if report.get("critical_error"):
        return f"🚨 *QA: критична помилка*\n\n{report['base_url']}\n\n{report['critical_error']}"

    pages_n = len(report["pages_checked"])
    broken_n = len(report["broken_pages"])
    slow_n = len(report["slow_pages"])
    forms_n = report["forms_total"]
    form_issues_n = len(report["form_issues"])
    broken_img_n = len(report["broken_images"])
    avg_ms = report.get("avg_response_ms")

    all_ok = broken_n == 0 and slow_n == 0 and form_issues_n == 0 and broken_img_n == 0
    header = "✅ *QA: усе гаразд*" if all_ok else "⚠️ *QA: знайдено проблеми*"

    lines = [
        header,
        f"\n🌐 {report['base_url']}",
        f"📄 Перевірено сторінок: {pages_n}",
        f"⚡ Середній час відповіді: {avg_ms} мс" if avg_ms is not None else "⚡ Час відповіді: н/д",
        f"📋 Форм знайдено: {forms_n}",
    ]

    if broken_n:
        lines.append(f"\n❌ *Биті сторінки ({broken_n}):*")
        for p in report["broken_pages"][:5]:
            lines.append(f"  • {p['url']} — статус {p['status']}")

    if slow_n:
        lines.append(f"\n🐢 *Повільні сторінки (>{SLOW_PAGE_MS} мс):*")
        for p in report["slow_pages"][:5]:
            lines.append(f"  • {p['url']} — {p['ms']} мс")

    if form_issues_n:
        lines.append(f"\n📋 *Проблеми з формами ({form_issues_n}):*")
        for issue in report["form_issues"][:5]:
            lines.append(f"  • {issue}")

    if broken_img_n:
        lines.append(f"\n🖼 *Биті зображення ({broken_img_n}):*")
        for img in report["broken_images"][:5]:
            lines.append(f"  • {img}")

    if all_ok:
        lines.append("\n💡 Кнопки й JS-поведінку цей скан не перевіряє — тільки HTTP/HTML рівень.")

    return "\n".join(lines)


# ============================================================
# НОВЕ: Моніторинг сторінок (контент-діф + AI-аналіз)
# ============================================================

PAGE_CONTENT_SNAPSHOT_CHARS = 6000   # скільки символів зберігати як знімок
PAGE_CONTENT_PROMPT_CHARS = 3000     # скільки символів старого/нового тексту слати в AI-промпт
_NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe"]

IMPORTANCE_ICON = {"low": "🟢", "none": "🟢", "medium": "🟡", "high": "🔴"}
FREQ_LABELS = {15: "15 хв", 30: "30 хв", 60: "1 год", 180: "3 год", 360: "6 год", 720: "12 год", 1440: "24 год"}
FREQ_OPTIONS = [15, 30, 60, 180, 360, 720, 1440]


def format_interval(minutes: int) -> str:
    return FREQ_LABELS.get(minutes, f"{minutes} хв")


def _extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fetch_page_content_text(url: str, timeout_seconds: int = 12) -> dict | None:
    """
    Повертає {"text": str} при успіху або {"error": str} якщо сторінку
    неможливо коректно завантажити/проаналізувати — щоб хендлер/scheduler
    чесно повідомили користувачу, а не вигадували зміни на порожніх даних.
    """
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout_seconds), allow_redirects=True
            ) as resp:
                if resp.status in (403, 429):
                    return {"error": f"сайт заблокував доступ (статус {resp.status})"}
                if resp.status >= 400:
                    return {"error": f"сторінка повернула помилку (статус {resp.status})"}
                html = await resp.text(errors="ignore")
    except Exception:
        logger.warning("Page content fetch failed for %s", url)
        return {"error": "сторінка недоступна або перевищено час очікування"}

    text = _extract_visible_text(html)
    if not text or len(text) < 20:
        return {"error": "не вдалося розпізнати текстовий вміст (можливо, потрібен JS для рендеру)"}

    return {"text": text[:PAGE_CONTENT_SNAPSHOT_CHARS]}


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def should_notify(watch: dict, importance: str) -> bool:
    if not watch.get("notifications_enabled", True):
        return False
    if importance == "high":
        return True
    if importance == "medium":
        return watch.get("notify_moderate", False)
    return False


async def analyze_page_change(url: str, old_text: str, new_text: str) -> dict | None:
    """Викликається ЛИШЕ коли хеш контенту вже змінився (перевіряється в
    scheduler/хендлері до виклику) — щоб не палити AI-запити на сторінках
    без змін."""
    if not ai_service.is_available():
        return None

    old_snip = old_text[:PAGE_CONTENT_PROMPT_CHARS]
    new_snip = new_text[:PAGE_CONTENT_PROMPT_CHARS]

    prompt = (
        "Ти аналізуєш зміни на веб-сторінці для системи моніторингу. "
        "Порівняй старий і новий вміст та визнач, чи це РЕАЛЬНА змістовна зміна, "
        "чи технічний шум (лічильник переглядів, поточна дата/час, id сесії, "
        "реклама, порядок елементів без зміни змісту тощо).\n\n"
        f"URL: {url}\n\n"
        f"БУЛО:\n{old_snip}\n\n"
        f"СТАЛО:\n{new_snip}\n\n"
        "Поверни ЛИШЕ JSON без пояснень:\n"
        '{"is_real_change": true/false, '
        '"importance": "low"/"medium"/"high", '
        '"change_type": "price"/"availability"/"content"/"design"/"other", '
        '"summary": "коротко одним реченням українською що змінилося", '
        '"before": "стисло що було українською", '
        '"after": "стисло що стало українською", '
        '"why_important": "чому ця зміна важлива користувачу (або чому неважлива), українською"}\n'
        'Якщо це шум — постав is_real_change: false, importance: "low".'
    )
    return await ai_service.generate_json(prompt, temperature=0.3)


async def build_ai_trend_report(url: str, history: list[dict]) -> dict | None:
    if not ai_service.is_available():
        return None

    entries = []
    for h in history[:30]:
        date = (h.get("checked_at") or "")[:10]
        entries.append(f"- {date} [{h.get('change_type', 'other')}/{h.get('importance', '')}]: {h.get('summary', '')}")
    history_text = "\n".join(entries)

    prompt = (
        "Ти аналізуєш накопичену історію змін веб-сторінки, яку моніторить користувач.\n\n"
        f"URL: {url}\n\nІсторія змін (від новіших до старіших):\n{history_text}\n\n"
        "На основі цієї історії зроби аналітичний висновок. Поверни ЛИШЕ JSON:\n"
        '{"trends": "які зміни відбуваються найчастіше, українською", '
        '"price_trend": "як змінюється ціна, або null якщо не стосується", '
        '"availability_pattern": "чи часто з’являються/зникають товари, або null", '
        '"content_pattern": "як змінюється контент сторінки, українською", '
        '"prediction": "що ймовірно зміниться найближчим часом на основі патерну, українською"}'
    )
    return await ai_service.generate_json(prompt, temperature=0.5)


def format_change_notification(url: str, label: str, importance: str, analysis: dict) -> str:
    icon = IMPORTANCE_ICON.get(importance, "🟡")
    return (
        f"{icon} *Зміна на сторінці* — {label}\n"
        f"🌐 {url}\n\n"
        f"*Було:*\n{analysis.get('before', '—')}\n\n"
        f"*Стало:*\n{analysis.get('after', '—')}\n\n"
        f"🤖 {analysis.get('summary', '')}\n{analysis.get('why_important', '')}"
    )


def format_history(label: str, history: list[dict]) -> str:
    if not history:
        return f"📭 Історія змін для «{label}» поки порожня."
    lines = [f"📜 *Історія змін* — {label}\n"]
    for h in history:
        icon = IMPORTANCE_ICON.get(h.get("importance"), "🟡")
        date = (h.get("checked_at") or "")[:16].replace("T", " ")
        lines.append(f"{icon} {date} — {h.get('summary', '')}")
    return "\n".join(lines)


def format_ai_report(label: str, report: dict) -> str:
    lines = [f"🤖 *AI-звіт* — {label}\n", f"📊 {report.get('trends', '—')}"]
    if report.get("price_trend"):
        lines.append(f"\n💰 Ціна: {report['price_trend']}")
    if report.get("availability_pattern"):
        lines.append(f"\n📦 Наявність: {report['availability_pattern']}")
    if report.get("content_pattern"):
        lines.append(f"\n📝 Контент: {report['content_pattern']}")
    if report.get("prediction"):
        lines.append(f"\n🔮 Прогноз: {report['prediction']}")
    return "\n".join(lines)


def format_global_stats(watches: list[dict]) -> str:
    total = len(watches)
    uptime_n = sum(1 for w in watches if w.get("kind", "uptime") == "uptime")
    page_n = total - uptime_n
    checks = sum(w.get("checks_count", 0) for w in watches)
    changes = sum(w.get("changes_count", 0) for w in watches)
    important = sum(w.get("important_changes_count", 0) for w in watches)

    last_change_dates = [w["last_change_at"] for w in watches if w.get("last_change_at")]
    last_change = max(last_change_dates)[:16].replace("T", " ") if last_change_dates else "ще не було"

    return (
        "📈 *Загальна статистика моніторингу*\n\n"
        f"🌐 Uptime-моніторингів: {uptime_n}\n"
        f"📄 Сторінок під наглядом: {page_n}\n"
        f"🔎 Всього перевірок: {checks}\n"
        f"🔄 Всього змін: {changes} (важливих: {important})\n"
        f"🕐 Остання зміна: {last_change}"
    )


def build_page_card_text(w: dict) -> str:
    label = w.get("label") or w["url"]
    checks = w.get("checks_count", 0)
    changes = w.get("changes_count", 0)
    important = w.get("important_changes_count", 0)
    last_change = w.get("last_change_at")
    last_change_text = last_change[:16].replace("T", " ") if last_change else "ще не було"
    notif_on = w.get("notifications_enabled", True)
    moderate_on = w.get("notify_moderate", False)
    freq = format_interval(w.get("check_interval_minutes", 60))
    unreachable = w.get("last_fetch_ok") is False

    text = (
        f"📄 *{label}*\n🌐 {w['url']}\n\n"
        f"🔎 Перевірок: {checks} | 🔄 Змін: {changes} (важливих: {important})\n"
        f"🕐 Остання зміна: {last_change_text}\n"
        f"⏱ Частота: {freq}\n"
        f"🔔 Сповіщення: {'увімкнено' if notif_on else 'вимкнено'}"
        + (f" (🟡 помірні: {'так' if moderate_on else 'ні'})" if notif_on else "")
    )
    if unreachable:
        text += "\n\n⚠️ Наразі сторінка недоступна для аналізу."
    return text