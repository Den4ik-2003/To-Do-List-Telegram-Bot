import logging
import re
from difflib import SequenceMatcher
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

CURL_HEADERS = {"Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7"}
IMPERSONATE = "chrome124"

# Синоніми/побутові формулювання -> нормалізовані ключові слова.
# AI вже вміє це розпізнавати сам через промпт нижче, цей словник — друга
# лінія захисту для точкового replace перед пошуком (напр. коли AI поверне
# щось надто буквальне).
PROFESSION_SYNONYMS = {
    "фронтенд": "frontend developer",
    "бекенд": "backend developer",
    "програміст на реакті": "react developer",
    "водій бусу": "водій категорії B",
    "робота з дому": "remote",
    "без досвіду": "no experience",
}


async def parse_job_query(user_text: str, profile: dict | None, feedback: list[dict] | None = None) -> dict | None:
    profile_text = ""
    if profile:
        profile_text = (
            f"Профіль користувача (використовуй як контекст, якщо запит не все уточнює): "
            f"професія={profile.get('profession','')}, досвід={profile.get('experience','')}, "
            f"навички={profile.get('skills','')}, бажана локація={profile.get('location','')}, "
            f"формат={profile.get('work_format','')}, зарплата={profile.get('desired_salary','')}"
        )

    feedback_text = ""
    if feedback:
        reasons = [f"{f.get('title','')} — причина: {f.get('reason','')}" for f in feedback[:5]]
        feedback_text = "Раніше користувач відхиляв схожі вакансії: " + "; ".join(reasons)

    prompt = f"""Проаналізуй запит користувача на пошук роботи і витягни критерії.
Розумій синоніми та побутові формулювання (напр. "фронтенд" = Frontend Developer,
"робота з дому" = remote, "без досвіду" = no experience).

Запит: "{user_text}"
{profile_text}
{feedback_text}

Поверни ЛИШЕ JSON:
{{
  "is_it": true/false (чи це IT-вакансія),
  "profession": "нормалізована назва професії/посади",
  "level": "junior|middle|senior|no_exp|" (порожньо якщо не вказано),
  "skills": ["навичка1", "навичка2"],
  "city": "місто або порожньо",
  "work_format": "remote|office|hybrid|" (порожньо якщо не важливо),
  "salary_min": число або null,
  "salary_currency": "USD|UAH",
  "employment_type": "full|part|",
  "search_keywords": ["1-3 короткі ключові слова для пошуку на джоб-бордах"]
}}"""
    return await ai_service.generate_json(prompt, temperature=0.3)


def _matches_criteria(text: str, criteria: dict) -> bool:
    text_l = text.lower()
    keywords = criteria.get("search_keywords") or [criteria.get("profession", "")]
    return any(kw.lower() in text_l for kw in keywords if kw)


# ... fetch_djinni / fetch_dou / fetch_workua / fetch_robotaua БЕЗ ЗМІН,
# залиш як у поточному файлі ...


def _normalize_for_dedup(v: dict) -> str:
    text = f"{v.get('title','')} {v.get('company','')}".lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_vacancies(vacancies: list[dict], threshold: float = 0.82) -> list[dict]:
    """
    Об'єднує вакансії, які схожі за назвою+компанією навіть якщо URL різні
    (та сама вакансія на Work.ua і Robota.ua). Для кожної групи лишає перший
    запис, але додає поле "sources" зі списком усіх джерел, де вона знайдена.
    """
    unique: list[dict] = []
    keys: list[str] = []

    for v in vacancies:
        key = _normalize_for_dedup(v)
        matched = False
        for i, existing_key in enumerate(keys):
            if SequenceMatcher(None, key, existing_key).ratio() >= threshold:
                existing = unique[i]
                sources = set(existing.get("sources", [existing.get("source", "")]))
                sources.add(v.get("source", ""))
                existing["sources"] = sorted(s for s in sources if s)
                matched = True
                break
        if not matched:
            v["sources"] = [v.get("source", "")]
            unique.append(v)
            keys.append(key)

    return unique


def apply_filters(vacancies: list[dict], filters: dict) -> list[dict]:
    result = vacancies
    if filters.get("remote_only"):
        result = [v for v in result if "remote" in (v.get("work_format") or "").lower()]
    if filters.get("city"):
        city = filters["city"].lower()
        result = [v for v in result if city in (v.get("location") or "").lower()]
    if filters.get("min_match") is not None:
        result = [
            v for v in result
            if (v.get("_score", {}).get("match_percent") or 0) >= filters["min_match"]
        ]
    return result


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
    unique_by_url = []
    for v in all_results:
        if v["url"] in seen_urls:
            continue
        seen_urls.add(v["url"])
        unique_by_url.append(v)

    return dedupe_vacancies(unique_by_url)


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
{{"match_percent": число 0-100, "fits": ["що підходить, коротко, 1-3 слова кожне"],
  "missing": ["чого не вистачає, коротко"],
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
Портфоліо: {profile.get('portfolio_url','')}
Резюме (короткий опис): {profile.get('resume_summary','')}

Напиши українською (якщо вакансія англомовна — англійською), 120-180 слів, без загальних
фраз на кшталт "я командний гравець". Конкретно, з посиланням на реальні навички кандидата
і вимоги вакансії. Поверни ЛИШЕ текст листа."""

    return await ai_service.generate_text(prompt, temperature=0.6)