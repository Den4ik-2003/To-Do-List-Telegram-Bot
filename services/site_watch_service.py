import logging
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

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
        method = (form.get("method") or "get").strip().lower()
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
    """
    Проводить QA-скан рівня HTTP/HTML: доступність головної та внутрішніх
    сторінок, биті посилання/зображення, наявність форм і базові проблеми
    в них, час відповіді кожної сторінки.

    НЕ виконує JS і не клікає по кнопках буквально — для цього потрібен
    headless-браузер (Playwright), якого зараз немає в стеку.
    """
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