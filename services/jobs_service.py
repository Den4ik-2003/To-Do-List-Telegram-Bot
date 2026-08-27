import logging
import re
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from services import ai_service

logger = logging.getLogger("tasks_bot")

DJINNI_RSS_URL = "https://djinni.co/jobs/rss/"
DOU_SEARCH_URL = "https://jobs.dou.ua/vacancies/?search={query}"
WORKUA_SEARCH_URL = "https://www.work.ua/jobs-{query}/"
ROBOTA_SEARCH_URL = "https://robota.ua/zapros/{query}/ukraine"

CURL_HEADERS = {
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
}
IMPERSONATE = "chrome124"


async def parse_job_query(user_text: str, profile: dict | None) -> dict | None:
    profile_text = ""
    if profile:
        profile_text = (
            f"Профіль користувача (використовуй як контекст, якщо запит не все уточнює): "
            f"професія={profile.get('profession','')}, досвід={profile.get('experience','')}, "
            f"навички={profile.get('skills','')}, бажана локація={profile.get('location','')}, "
            f"формат={profile.get('work_format','')}, зарплата={profile.get('desired_salary','')}"
        )

    prompt = f"""Проаналізуй запит користувача на пошук роботи і витягни критерії.
Запит: "{user_text}"
{profile_text}

Поверни ЛИШЕ JSON:
{{
  "is_it": true/false (чи це IT-вакансія),
  "profession": "назва професії/посади",
  "level": "junior|middle|senior|no_exp|" (порожньо якщо не вказано),
  "skills": ["навичка1", "навичка2"],
  "city": "місто або порожньо",
  "work_format": "remote|office|hybrid|" (порожньо якщо не важливо),
  "salary_min": число або null,
  "salary_currency": "USD|UAH|" ,
  "employment_type": "full|part|" (повна/неповна зайнятість, порожньо якщо не вказано),
  "search_keywords": ["1-3 короткі ключові слова для пошуку на джоб-бордах"]
}}"""
    return await ai_service.generate_json(prompt, temperature=0.3)


def _matches_criteria(text: str, criteria: dict) -> bool:
    text_l = text.lower()
    keywords = criteria.get("search_keywords") or [criteria.get("profession", "")]
    return any(kw.lower() in text_l for kw in keywords if kw)


async def fetch_djinni(criteria: dict, limit: int = 30) -> list[dict]:
    if not criteria.get("is_it"):
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DJINNI_RSS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                raw = await resp.text()
    except Exception:
        logger.exception("Djinni RSS fetch failed")
        return []

    soup = BeautifulSoup(raw, "xml")
    items = soup.find_all("item")
    results = []
    for item in items:
        title = item.find("title").get_text(strip=True) if item.find("title") else ""
        link = item.find("link").get_text(strip=True) if item.find("link") else ""
        desc = item.find("description").get_text(strip=True) if item.find("description") else ""
        combined = f"{title} {desc}"
        if not _matches_criteria(combined, criteria):
            continue
        results.append({
            "id": link,
            "title": title,
            "company": "",
            "location": "Remote/Ukraine",
            "work_format": "remote" if "remote" in desc.lower() else "",
            "salary": "",
            "url": link,
            "source": "Djinni",
            "requirements": BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)[:600],
        })
        if len(results) >= limit:
            break
    return results


async def fetch_dou(criteria: dict, limit: int = 15) -> list[dict]:
    if not criteria.get("is_it"):
        return []
    query = " ".join(criteria.get("search_keywords") or [criteria.get("profession", "")])
    url = DOU_SEARCH_URL.format(query=quote(query))
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=CURL_HEADERS) as session:
            resp = await session.get(url, timeout=20)
            if resp.status_code != 200:
                logger.warning("DOU search status=%s", resp.status_code)
                return []
            html = resp.text
    except Exception:
        logger.exception("DOU search failed for %s", url)
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".l-vacancy")
    results = []
    for card in cards[:limit]:
        title_el = card.select_one(".title .vt")
        company_el = card.select_one(".title .company")
        date_el = card.select_one(".date")
        link = title_el.get("href") if title_el else None
        if not link:
            continue
        results.append({
            "id": link,
            "title": title_el.get_text(strip=True) if title_el else "",
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": "",
            "work_format": "",
            "salary": "",
            "url": link,
            "source": "DOU",
            "requirements": date_el.get_text(strip=True) if date_el else "",
        })
    return results


async def fetch_workua(criteria: dict, limit: int = 15) -> list[dict]:
    query = "+".join(criteria.get("search_keywords") or [criteria.get("profession", "")])
    city = criteria.get("city", "").strip()
    slug = f"{city.lower()}-{query}" if city else query
    url = WORKUA_SEARCH_URL.format(query=quote(slug))
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=CURL_HEADERS) as session:
            resp = await session.get(url, timeout=20)
            if resp.status_code != 200:
                logger.warning("Work.ua search status=%s", resp.status_code)
                return []
            html = resp.text
    except Exception:
        logger.exception("Work.ua search failed for %s", url)
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.card.card-hover")
    results = []
    for card in cards[:limit]:
        title_el = card.select_one("h2 a")
        company_el = card.select_one("span.strong-600") or card.select_one("[title]")
        salary_el = card.select_one("span.text-muted-print.strong-600.nowrap")
        link = title_el.get("href") if title_el else None
        if not link or not title_el:
            continue
        full_link = "https://www.work.ua" + link if link.startswith("/") else link
        results.append({
            "id": full_link,
            "title": title_el.get_text(strip=True),
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": city or "",
            "work_format": "",
            "salary": salary_el.get_text(strip=True) if salary_el else "",
            "url": full_link,
            "source": "Work.ua",
            "requirements": "",
        })
    return results


async def fetch_robotaua(criteria: dict, limit: int = 15) -> list[dict]:
    """
    Найризикованіше джерело — Robota.ua не має підтвердженого публічного API
    для пошуку. Це best-effort скрапінг публічної сторінки видачі; якщо
    структура сайту зміниться, ця функція може почати повертати порожній
    список без явної помилки. Якщо це трапиться — потрібно буде оновити
    селектори під актуальну розмітку сайту.
    """
    query = criteria.get("profession", "") or (criteria.get("search_keywords") or [""])[0]
    url = ROBOTA_SEARCH_URL.format(query=quote(query))
    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=CURL_HEADERS) as session:
            resp = await session.get(url, timeout=20)
            if resp.status_code != 200:
                logger.warning("Robota.ua search status=%s", resp.status_code)
                return []
            html = resp.text
    except Exception:
        logger.exception("Robota.ua search failed for %s", url)
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("[data-qa='vacancy-serp-item']") or soup.select("article")
    results = []
    for card in cards[:limit]:
        title_el = card.find("a")
        if not title_el:
            continue
        link = title_el.get("href", "")
        full_link = "https://robota.ua" + link if link.startswith("/") else link
        results.append({
            "id": full_link,
            "title": title_el.get_text(strip=True),
            "company": "",
            "location": criteria.get("city", ""),
            "work_format": "",
            "salary": "",
            "url": full_link,
            "source": "Robota.ua",
            "requirements": "",
        })
    return results


async def search_vacancies(criteria: dict) -> list[dict]:
    sources = [fetch_workua(criteria), fetch_robotaua(criteria)]
    if criteria.get("is_it"):
        sources = [fetch_djinni(criteria), fetch_dou(criteria)] + sources

    all_results = []
    for coro in sources:
        try:
            all_results.extend(await coro)
        except Exception:
            logger.exception("A job source failed during search_vacancies")

    seen_urls = set()
    unique = []
    for v in all_results:
        if v["url"] in seen_urls:
            continue
        seen_urls.add(v["url"])
        unique.append(v)
    return unique


async def score_vacancy(vacancy: dict, profile: dict | None) -> dict:
    if not ai_service.is_available() or not profile:
        return {"match_percent": None, "fits": [], "missing": [], "highlight": "", "advice": ""}

    prompt = f"""Оціни відповідність вакансії профілю кандидата.

Вакансія: {vacancy.get('title')} у {vacancy.get('company') or 'компанії'}.
Вимоги/опис: {vacancy.get('requirements', '')[:800]}

Профіль кандидата:
Професія: {profile.get('profession','')}
Досвід: {profile.get('experience','')}
Навички: {profile.get('skills','')}
Освіта: {profile.get('education','')}
Мови: {profile.get('languages','')}

Поверни ЛИШЕ JSON:
{{"match_percent": число 0-100, "fits": ["що підходить"], "missing": ["чого не вистачає"],
  "highlight": "що варто підкреслити в заявці", "advice": "чи варто подаватися, одне речення"}}"""

    result = await ai_service.generate_json(prompt, temperature=0.4)
    return result or {"match_percent": None, "fits": [], "missing": [], "highlight": "", "advice": ""}


async def generate_cover_letter(vacancy: dict, profile: dict) -> str | None:
    prompt = f"""Напиши персональний Cover Letter під конкретну вакансію. НЕ роби шаблонним —
адаптуй саме під цю вакансію і профіль кандидата.

Вакансія: {vacancy.get('title')} у {vacancy.get('company') or 'компанії'}
Вимоги: {vacancy.get('requirements', '')[:800]}

Профіль кандидата:
Професія: {profile.get('profession','')}
Досвід: {profile.get('experience','')}
Навички: {profile.get('skills','')}
Освіта: {profile.get('education','')}
Мови: {profile.get('languages','')}
Резюме (короткий опис): {profile.get('resume_summary','')}

Напиши українською (якщо вакансія англомовна — англійською), 120-180 слів, без загальних
фраз на кшталt "я командний гравець". Конкретно, з посиланням на реальні навички кандидата
і вимоги вакансії. Поверни ЛИШЕ текст листа."""

    return await ai_service.generate_text(prompt, temperature=0.6)