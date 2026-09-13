import json
import logging
import re
from datetime import datetime, timedelta

from config.constants import LABELS, CATEGORIES
from config.settings import WORK_HOURS_TEXT
from services import ai_service

logger = logging.getLogger("tasks_bot")

MAX_DRAFTS = 8

WEEKDAY_STEMS = [
    ("понеділ", 0), ("вівтор", 1), ("серед", 2), ("четвер", 3),
    ("п'ятниц", 4), ("пятниц", 4), ("субот", 5), ("неділ", 6),
]


def _find_weekday(t: str) -> int | None:
    for stem, wd in WEEKDAY_STEMS:
        if stem in t:
            return wd
    return None


def _deterministic_date(text: str, now: datetime):
    """Надійний детермінований override для найпоширеніших фраз. Якщо нічого
    не збіглось — повертає None, і рішення лишається за AI (date_iso)."""
    t = text.lower()
    if "післязавтра" in t:
        return (now + timedelta(days=2)).date()
    if "через тиждень" in t:
        return (now + timedelta(days=7)).date()
    m = re.search(r"через\s+(\d+)\s+д", t)
    if m:
        return (now + timedelta(days=int(m.group(1)))).date()
    if "завтра" in t:
        return (now + timedelta(days=1)).date()
    if "сьогодні" in t:
        return now.date()

    wd = _find_weekday(t)
    if wd is not None:
        days_ahead = (wd - now.weekday()) % 7
        if "наступн" in t and days_ahead == 0:
            days_ahead = 7
        return (now + timedelta(days=days_ahead)).date()

    return None


def _parse_work_hours_end(work_hours_text: str):
    m = re.search(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})", work_hours_text or "")
    if not m:
        return None
    return int(m.group(3)), int(m.group(4))


def _resolve_period_time(period: str, work_hours_text: str):
    """Повертає ((год, хв), estimated_bool) або (None, False), якщо період
    не можна безпечно перетворити в конкретний час (напр. 'ввечері')."""
    if period == "ranok":
        return (8, 0), True
    if period == "pislya_roboty":
        end = _parse_work_hours_end(work_hours_text) or (18, 0)
        total = end[0] * 60 + end[1] + 30
        return (total // 60 % 24, total % 60), True
    if period == "do_kincya_dnya":
        end = _parse_work_hours_end(work_hours_text) or (18, 0)
        return end, True
    return None, False


def _safe_label(v):
    return v if v in LABELS else None


def _safe_category(v):
    return v if v in CATEGORIES else None


def _match_project(hint: str | None, projects: list):
    """Повертає (project_or_None, candidates_list). Ніколи не вигадує
    проєкт — тільки точний або однозначний частковий збіг."""
    if not hint:
        return None, []
    hint_norm = hint.strip().lower()
    exact = [p for p in projects if p.get("title", "").strip().lower() == hint_norm]
    if exact:
        return exact[0], []
    partial = [
        p for p in projects
        if hint_norm in p.get("title", "").lower() or p.get("title", "").lower() in hint_norm
    ]
    if len(partial) == 1:
        return partial[0], []
    if len(partial) > 1:
        return None, partial
    return None, []


def _build_prompt(text: str, projects: list, now: datetime) -> str:
    weekday_ua = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"][now.weekday()]
    projects_list_text = "\n".join(f"- {p.get('title', '')}" for p in projects) or "(активних проєктів немає)"

    return f"""Ти — модуль розпізнавання задач з голосового повідомлення українською мовою.
Поточна дата: {now.strftime('%d.%m.%Y')} ({weekday_ua}), поточний час: {now.strftime('%H:%M')}.
Робочий графік користувача: {WORK_HOURS_TEXT}.

Активні проєкти користувача (обирай project_hint ТІЛЬКИ з цього списку, або null):
{projects_list_text}

Розшифровка голосового повідомлення користувача:
"{text}"

Визнач, чи це повідомлення описує одну або декілька задач для to-do списку.
Якщо це НЕ схоже на задачу (розмова, питання, коментар) — поверни "is_task": false, "tasks": [].

Якщо це задача(і) — витягни для КОЖНОЇ:
- text: коротка конкретна назва дії (без дати/часу/пріоритету в тексті)
- description: додатковий контекст, якщо був сказаний, інакше ""
- label: одне з ["urgent","medium","low","idea","personal"] або null, якщо пріоритет не сказаний
- category: одне з ["work","finance","home","sport","study","business","idea","other"] або null
- date_iso: "YYYY-MM-DD", ТІЛЬКИ якщо дата однозначно випливає з мови; інакше null. НЕ вигадуй.
- time: "HH:MM", ТІЛЬКИ якщо сказано конкретний час; інакше null
- time_period: якщо час — це період, а не хвилина: одне з ["ranok","vechir","pislya_roboty","do_kincya_dnya"], інакше null
- duration_minutes: ціле число хвилин, ТІЛЬКИ якщо тривалість сказана ("пів години"=30, "годину"=60, "20 хвилин"=20); інакше null. НЕ вигадуй.
- project_hint: точна назва проєкту зі списку вище, якщо згадувався, інакше null
- confidence: "high", якщо всі ключові поля зрозумілі однозначно; "low" — якщо є суттєва невизначеність

Поверни ЛИШЕ JSON без пояснень:
{{"is_task": true|false, "tasks": [ {{...}}, ... ]}}"""


async def parse_voice_tasks(text: str, projects: list) -> dict:
    """Повертає {"ok": bool, "is_task": bool|None, "drafts": [...]}."""
    now = datetime.now()
    data = await ai_service.generate_json(_build_prompt(text, projects, now), temperature=0.3)
    if not data:
        return {"ok": False, "is_task": None, "drafts": []}

    is_task = bool(data.get("is_task"))
    raw_tasks = data.get("tasks") or []
    if not is_task or not raw_tasks or not isinstance(raw_tasks, list):
        return {"ok": True, "is_task": False, "drafts": []}

    drafts = []
    for raw in raw_tasks[:MAX_DRAFTS]:
        if not isinstance(raw, dict):
            continue
        task_text = str(raw.get("text") or "").strip()[:200]
        if not task_text:
            continue

        label = _safe_label(raw.get("label")) or "medium"
        category = _safe_category(raw.get("category")) or "other"

        the_date = _deterministic_date(text, now)
        if the_date is None and raw.get("date_iso"):
            try:
                the_date = datetime.strptime(raw["date_iso"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                the_date = None

        the_time = None
        time_is_estimated = False
        time_str = raw.get("time")
        if time_str:
            try:
                datetime.strptime(time_str, "%H:%M")
                the_time = time_str
            except (ValueError, TypeError):
                the_time = None
        period = raw.get("time_period")
        if the_time is None and period:
            resolved, estimated = _resolve_period_time(period, WORK_HOURS_TEXT)
            if resolved:
                the_time = f"{resolved[0]:02d}:{resolved[1]:02d}"
                time_is_estimated = estimated

        due = ""
        date_had_no_time = False
        if the_date and the_time:
            due = f"{the_date.strftime('%d.%m.%Y')} {the_time}"
        elif the_date and not the_time:
            due = ""  # дата є, час не визначився однозначно — не вигадуємо хвилину
            date_had_no_time = True

        duration = raw.get("duration_minutes")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None:
            duration = max(5, min(duration, 480))

        project_obj, candidates = _match_project(raw.get("project_hint"), projects)

        confidence = raw.get("confidence") if raw.get("confidence") in ("high", "low") else "low"
        ambiguous = confidence == "low" or bool(candidates) or date_had_no_time

        drafts.append({
            "text": task_text,
            "description": str(raw.get("description") or "").strip()[:300],
            "label": label,
            "category": category,
            "due": due,
            "date_had_no_time": date_had_no_time,
            "time_is_estimated": time_is_estimated,
            "estimated_minutes": duration,
            "project_id": str(project_obj["_id"]) if project_obj else None,
            "project_title": project_obj.get("title") if project_obj else None,
            "project_candidates": [{"id": str(p["_id"]), "title": p.get("title", "")} for p in candidates],
            "ambiguous": ambiguous,
        })

    return {"ok": True, "is_task": bool(drafts), "drafts": drafts}