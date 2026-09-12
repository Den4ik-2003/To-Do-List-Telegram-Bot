import json
import logging
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from urllib.parse import quote, quote_plus

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from services import ai_service

logger = logging.getLogger("tasks_bot")

DOU_RSS_URL = "https://jobs.dou.ua/vacancies/feeds/?search={query}"
DJINNI_RSS_URL = "https://djinni.co/jobs/rss/?primary_keyword={query}"
WORKUA_SEARCH_URL = "https://www.work.ua/jobs-{query}/"
WORKUA_SEARCH_CITY_URL = "https://www.work.ua/jobs-{city}-{query}/"
ROBOTA_SEARCH_URL = "https://robota.ua/zapros/{query}/ukraine"

CURL_HEADERS = {
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
IMPERSONATE = "chrome124"
REQUEST_TIMEOUT = 15

PROFESSION_SYNONYMS = {
    "фронтенд": "frontend developer",
    "бекенд": "backend developer",
    "програміст на реакті": "react developer",
    "водій бусу": "водій категорії B",
    "робота з дому": "remote",
    "без досвіду": "no experience",
}

WORKUA_CITY_SLUGS = {
    "київ": "kyiv", "kyiv": "kyiv", "киев": "kyiv",
    "дніпро": "dnipro", "днепр": "dnipro", "dnipro": "dnipro",
    "львів": "lviv", "львов": "lviv", "lviv": "lviv",
    "одеса": "odesa", "одесса": "odesa", "odesa": "odesa",
    "харків": "kharkiv", "харьков": "kharkiv", "kharkiv": "kharkiv",
    "черкаси": "cherkasy", "cherkasy": "cherkasy",
    "чернігів": "chernihiv", "chernihiv": "chernihiv",
    "чернівці": "chernivtsi_cv",
    "донецьк": "donetsk",
    "івано-франківськ": "ivano-frankivsk",
    "херсон": "kherson",
    "хмельницький": "khmelnytskyi",
    "кропивницький": "kropyvnytskyi",
    "луганськ": "luhansk",
    "луцьк": "lutsk",
    "миколаїв": "mykolaiv_nk",
    "полтава": "poltava",
    "рівне": "rivne",
    "сімферополь": "simferopol",
    "суми": "sumy",
    "тернопіль": "ternopil",
    "ужгород": "uzhhorod",
    "вінниця": "vinnytsya",
    "запоріжжя": "zaporizhzhya",
    "житомир": "zhytomyr",
}

_UA_CITY_NAMES = [
    "київ", "kyiv", "дніпро", "dnipro", "львів", "lviv", "одеса", "odesa",
    "харків", "kharkiv", "черкаси", "cherkasy", "чернігів", "chernihiv",
    "чернівці", "chernivtsi", "донецьк", "donetsk", "івано-франківськ",
    "херсон", "kherson", "хмельницький", "khmelnytskyi", "кропивницький",
    "луганськ", "luhansk", "луцьк", "lutsk", "миколаїв", "mykolaiv",
    "полтава", "poltava", "рівне", "rivne", "сімферополь", "simferopol",
    "суми", "sumy", "тернопіль", "ternopil", "ужгород", "uzhhorod",
    "вінниця", "vinnytsia", "запоріжжя", "zaporizhzhia", "житомир", "zhytomyr",
    "вся україна", "all ukraine", "remote (", "kyiv,",
]

_JOB_LINK_RE = re.compile(r"/jobs/\d+/?$")

_SALARY_RE = re.compile(
    r"\d[\d\s\u00a0]{2,}\s*(?:–|-|—)\s*\d[\d\s\u00a0]{2,}\s*(?:UAH|грн|₴)"
    r"|\d[\d\s\u00a0]{2,}\s*(?:UAH|грн|₴)"
    r"|\$\s?\d[\d\s\u00a0]{2,}(?:\s*(?:–|-|—)\s*\$?\s?\d[\d\s\u00a0]{2,})?",
    re.IGNORECASE,
)
_EXPERIENCE_RE = re.compile(
    r"(no experience|without experience|без досвіду|"
    r"experience (?:of )?(?:more than|less than)? ?\d+\+? ?years?|"
    r"\d+\+? ?years? of experience|"
    r"досвід(?: роботи)? (?:понад|більше|від) ?\d+\+? ?рок\w*|"
    r"(?:від |понад |більше )?\d+\+? ?рок\w* досвіду)",
    re.IGNORECASE,
)
_REMOTE_RE = re.compile(r"\b(remote|віддалено|дистанційно)\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\b(hybrid|гібрид\w*)\b", re.IGNORECASE)
_COMPANY_PREFIX_RE = re.compile(r"^([A-ZА-ЯІЇЄҐ][\w \-&\.']{1,40})\s*[—–-]\s+")

_CARD_NOISE_LINES = {
    "save", "already saved", "sign in", "register", "contact: phone",
    "зберегти", "вже збережено", "увійти", "зареєструватися",
    "to save a job, you need to sign in or register.",
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
"робота з дому"/"онлайн" = remote, "без досвіду" = no experience).

ВАЖЛИВО про search_keywords: це мають бути 1-3 РЕАЛЬНІ назви посад/професій, які можна
шукати на джоб-бордах (Djinni, Work.ua, Robota.ua) — НІКОЛИ не клади туди слова на кшталт
"вечір", "ніч", "вдень", "онлайн", "дистанційно" (вони відносяться до work_format/
employment_type, а не до назви професії, і як пошуковий запит дадуть 0 результатів).
Якщо користувач не назвав конкретну професію, а лише формат/час роботи (напр. "хочу
підробіток на вечір онлайн"), сам добери 2-3 РЕАЛЬНІ, поширені назви вакансій, що типово
підходять під такий формат (напр. оператор кол-центру, менеджер з продажу онлайн,
модератор чату, копірайтер, репетитор онлайн — обери релевантні контексту), і поклади
саме їх у search_keywords.

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
  "search_keywords": ["1-3 РЕАЛЬНІ назви професій для пошуку на джоб-бордах"]
}}"""
    return await ai_service.generate_json(prompt, temperature=0.3)


def _search_slug(criteria: dict) -> str:
    keywords = criteria.get("search_keywords") or [criteria.get("profession", "")]
    query = " ".join(k for k in keywords if k).strip() or criteria.get("profession", "")
    for src, dst in PROFESSION_SYNONYMS.items():
        query = query.replace(src, dst)
    return query.strip()


def _enrich_from_text(item: dict, text: str) -> dict:
    if not text:
        return item
    if item.get("salary") is None:
        m = _SALARY_RE.search(text)
        if m:
            item["salary"] = m.group(0).strip()
    if item.get("experience") is None:
        m = _EXPERIENCE_RE.search(text)
        if m:
            item["experience"] = m.group(0).strip()
    if item.get("work_format") is None:
        if _REMOTE_RE.search(text):
            item["work_format"] = "remote"
        elif _HYBRID_RE.search(text):
            item["work_format"] = "hybrid"
    return item


def _extract_company_prefix(text: str) -> str | None:
    if not text:
        return None
    m = _COMPANY_PREFIX_RE.match(text.strip())
    return m.group(1).strip() if m else None


def _strip_rss_html(raw: str) -> str:
    if not raw:
        return ""
    try:
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    except Exception:
        return raw


def _rss_findtext(item: ET.Element, tag: str) -> str:
    for child in item:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local.lower() == tag:
            return (child.text or "").strip()
    return ""


def _looks_like_xml(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    if not head:
        return False
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return False
    return head.startswith("<?xml") or head.startswith("<rss") or head.startswith("<feed")


async def _fetch_rss_items(url: str, source_name: str) -> list[dict]:
    try:
        async with aiohttp.ClientSession(headers=CURL_HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status != 200:
                    logger.warning("%s RSS status=%s for %s", source_name, resp.status, url)
                    return []
                xml_text = await resp.text()
    except Exception:
        logger.exception("%s RSS fetch failed for %s", source_name, url)
        return []

    if not _looks_like_xml(xml_text):
        logger.warning("%s RSS returned non-XML content for %s", source_name, url)
        return []

    try:
        root = ET.fromstring(xml_text)
        items = root.findall(".//item")
    except ET.ParseError:
        logger.exception("%s RSS parse failed", source_name)
        return []

    results = []
    for item in items:
        title = _rss_findtext(item, "title")
        link = _rss_findtext(item, "link")
        description = _strip_rss_html(_rss_findtext(item, "description"))

        if not title or not link:
            continue

        entry = {
            "id": link,
            "url": link,
            "title": title,
            "company": _extract_company_prefix(description) if source_name == "Djinni" else None,
            "location": None,
            "work_format": None,
            "salary": None,
            "experience": None,
            "requirements": description[:1000],
            "source": source_name,
        }
        _enrich_from_text(entry, description)
        results.append(entry)

    return results


async def fetch_djinni(criteria: dict) -> list[dict]:
    query = _search_slug(criteria)
    if not query:
        return []
    url = DJINNI_RSS_URL.format(query=quote_plus(query))
    return await _fetch_rss_items(url, "Djinni")


async def fetch_dou(criteria: dict) -> list[dict]:
    query = _search_slug(criteria)
    if not query:
        return []
    url = DOU_RSS_URL.format(query=quote(query))
    return await _fetch_rss_items(url, "DOU")


def _extract_first_link_text(soup: BeautifulSoup, href_pattern: str) -> list[tuple[str, str]]:
    pairs = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href_pattern not in href:
            continue
        text = a.get_text(strip=True)
        if not text or href in seen:
            continue
        seen.add(href)
        pairs.append((text, href))
    return pairs


def _workua_url(criteria: dict) -> str | None:
    query = _search_slug(criteria)
    if not query:
        return None
    slug = quote(query.replace(" ", "+").lower())
    city_raw = (criteria.get("city") or "").strip().lower()
    city_slug = WORKUA_CITY_SLUGS.get(city_raw)
    if city_slug:
        return WORKUA_SEARCH_CITY_URL.format(city=city_slug, query=slug)
    return WORKUA_SEARCH_URL.format(query=slug)


def _find_card_container(a_tag, title: str):
    node = a_tag
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        node = parent
        text = node.get_text(" ", strip=True)
        if title not in text:
            continue
        job_links = [
            x for x in node.find_all("a", href=True)
            if _JOB_LINK_RE.search(x["href"].split("?")[0])
        ]
        if len(job_links) == 1 and 40 <= len(text) <= 2500:
            return node
    return a_tag.parent or a_tag


def _looks_like_city(line: str) -> bool:
    low = line.lower()
    if any(city in low for city in _UA_CITY_NAMES):
        return True
    if "km from center" in low or "км від центру" in low:
        return True
    return False


def _parse_workua_card_lines(lines: list[str], title: str) -> dict:
    result = {"company": None, "location": None, "work_format": None,
              "salary": None, "experience": None, "description": ""}
    desc_parts = []
    seen_title = False
    for line in lines:
        if not seen_title:
            if line == title:
                seen_title = True
            continue
        if line.strip().lower() in _CARD_NOISE_LINES:
            continue
        if result["salary"] is None and _SALARY_RE.search(line):
            result["salary"] = line.strip()
            continue
        if result["experience"] is None and _EXPERIENCE_RE.search(line):
            result["experience"] = _EXPERIENCE_RE.search(line).group(0).strip()
            continue
        if result["work_format"] is None:
            if _REMOTE_RE.search(line):
                result["work_format"] = "remote"
            elif _HYBRID_RE.search(line):
                result["work_format"] = "hybrid"
        if result["location"] is None and _looks_like_city(line):
            result["location"] = line.strip()
            continue
        if result["company"] is None and len(line) < 80 and len(line.split()) <= 8:
            result["company"] = line.strip()
            continue
        desc_parts.append(line)
    result["description"] = " ".join(desc_parts)
    return result


async def fetch_workua(criteria: dict) -> list[dict]:
    url = _workua_url(criteria)
    if not url:
        return []

    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=CURL_HEADERS) as session:
            resp = await session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.warning("Work.ua status=%s for %s", resp.status_code, url)
                return []
            html = resp.text
    except Exception:
        logger.exception("Work.ua fetch failed for %s", url)
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.exception("Work.ua parse failed for %s", url)
        return []

    results = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _JOB_LINK_RE.search(href.split("?")[0]):
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 6 or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        full_url = href if href.startswith("http") else f"https://www.work.ua{href}"

        card = _find_card_container(a, title)
        card_lines = list(card.stripped_strings)
        parsed = _parse_workua_card_lines(card_lines, title)

        results.append({
            "id": full_url,
            "url": full_url,
            "title": title,
            "company": parsed["company"],
            "location": parsed["location"],
            "work_format": parsed["work_format"],
            "salary": parsed["salary"],
            "experience": parsed["experience"],
            "requirements": parsed["description"][:1000],
            "source": "Work.ua",
        })

    if not results:
        logger.info("Work.ua: 0 результатів для запиту %r (url=%s)", _search_slug(criteria), url)
    return results[:20]


def _extract_robotaua_from_embedded_json(html: str) -> list[dict]:
    scripts = re.findall(r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
    scripts += re.findall(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)

    results = []
    for raw in scripts:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        results.extend(_walk_json_for_vacancies(data))
    return results


def _walk_json_for_vacancies(node, depth: int = 0) -> list[dict]:
    if depth > 6:
        return []
    found = []
    if isinstance(node, dict):
        vid = node.get("vacancyId") or (node.get("id") if node.get("title") else None)
        title = node.get("title") or node.get("vacancyTitle")
        if vid and title and (node.get("companyName") or node.get("employerName") or node.get("url")):
            company = node.get("companyName") or node.get("employerName")
            raw_url = node.get("url") or (f"https://robota.ua/vacancy{vid}" if str(vid).isdigit() else None)
            if raw_url:
                found.append({
                    "id": str(vid),
                    "url": raw_url,
                    "title": title,
                    "company": company,
                    "location": node.get("cityName") or node.get("city"),
                    "work_format": "remote" if node.get("isRemoteWork") else None,
                    "salary": _format_robota_salary(node.get("salary")),
                    "experience": node.get("experienceName") or node.get("experience"),
                    "requirements": (node.get("description") or "")[:1000],
                    "source": "Robota.ua",
                })
        for v in node.values():
            found.extend(_walk_json_for_vacancies(v, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_json_for_vacancies(item, depth + 1))
    return found


def _format_robota_salary(value) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return f"{value:.0f} грн"
    return str(value)


async def fetch_robotaua(criteria: dict) -> list[dict]:
    query = _search_slug(criteria)
    if not query:
        return []
    slug = quote(query.replace(" ", "-").lower())
    url = ROBOTA_SEARCH_URL.format(query=slug)

    try:
        async with AsyncSession(impersonate=IMPERSONATE, headers=CURL_HEADERS) as session:
            resp = await session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.warning("Robota.ua status=%s for %s", resp.status_code, url)
                return []
            html = resp.text
    except Exception:
        logger.exception("Robota.ua fetch failed for %s", url)
        return []

    results = _extract_robotaua_from_embedded_json(html)
    if results:
        return results[:20]

    try:
        soup = BeautifulSoup(html, "html.parser")
        pairs = _extract_first_link_text(soup, "/vacancy")
    except Exception:
        logger.exception("Robota.ua parse failed for %s", url)
        return []

    results = []
    for title, href in pairs[:20]:
        if len(title) < 8:
            continue
        full_url = href if href.startswith("http") else f"https://robota.ua{href}"
        results.append({
            "id": full_url,
            "url": full_url,
            "title": title,
            "company": None,
            "location": None,
            "work_format": None,
            "salary": None,
            "experience": None,
            "requirements": "",
            "source": "Robota.ua",
        })

    if not results:
        logger.info(
            "Robota.ua: 0 результатів для запиту %r — сайт рендерить список вакансій "
            "через JS і не має публічного keyword-search API (лише per-company endpoint).",
            query,
        )
    return results


def _normalize_for_dedup(v: dict) -> str:
    text = f"{v.get('title','')} {v.get('company','')}".lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_vacancies(vacancies: list[dict], threshold: float = 0.82) -> list[dict]:
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
                for field in ("company", "salary", "location", "work_format", "experience"):
                    if not existing.get(field) and v.get(field):
                        existing[field] = v[field]
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


async def _search_once(criteria: dict) -> list[dict]:
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


async def search_vacancies(criteria: dict) -> list[dict]:
    vacancies = await _search_once(criteria)
    if vacancies:
        return vacancies

    keywords = criteria.get("search_keywords") or []
    if len(keywords) > 1 or criteria.get("city"):
        broadened = dict(criteria)
        broadened["search_keywords"] = keywords[:1] if keywords else [criteria.get("profession", "")]
        broadened["city"] = ""
        logger.info("search_vacancies: 0 результатів, пробую ширший запит %r", broadened["search_keywords"])
        vacancies = await _search_once(broadened)

    return vacancies


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


async def analyze_vacancy_full(vacancy: dict, profile: dict | None) -> str | None:
    if not ai_service.is_available():
        return None

    if profile:
        profile_text = f"""Профіль кандидата:
Професія: {profile.get('profession','')}
Досвід: {profile.get('experience','')}
Навички: {profile.get('skills','')}
Освіта: {profile.get('education','')}
Мови: {profile.get('languages','')}"""
    else:
        profile_text = ("Профіль кандидата не заповнений — оціни вакансію узагальнено, "
                         "без прив'язки до конкретних навичок кандидата, і в п.3 чесно "
                         "напиши, що для персональної оцінки навичок треба заповнити профіль.")

    sources = ", ".join(vacancy.get("sources") or [vacancy.get("source", "")])

    prompt = f"""Зроби детальний AI-аналіз вакансії для кандидата, що шукає роботу.

Вакансія: {vacancy.get('title')} у {vacancy.get('company') or 'компанії не вказано'}
Зарплата: {vacancy.get('salary') or 'не вказана'}
Локація/формат: {vacancy.get('location') or ''} {vacancy.get('work_format') or ''}
Досвід: {vacancy.get('experience') or 'не вказано'}
Опис/вимоги: {(vacancy.get('requirements') or '')[:1500]}
Джерело: {sources}

{profile_text}

Дай структурований аналіз у форматі (кожен пункт — окремий emoji-заголовок):
🎯 Наскільки підходить — коротко, з орієнтовним % якщо можеш оцінити
📋 Що конкретно вимагають — 3-6 пунктів
✅ Навички кандидата, що підходять — список (або "профіль не заповнено")
⚠️ Чого не вистачає — список (або "профіль не заповнено")
📊 Рівень вакансії — junior/middle/senior і чому саме так
👍 Плюси
👎 Мінуси
💡 Висновок — чи варто подаватися, одне чітке речення

Пиши українською, стисло і по суті, без вступних фраз."""

    return await ai_service.generate_text(prompt, temperature=0.4)


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