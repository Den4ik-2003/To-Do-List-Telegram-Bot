"""
ЗМІНЕНИЙ ФАЙЛ: database/job_profile.py

НОВЕ (розширений профіль + фільтр збігу):
- PROFILE_FIELDS / PROFILE_LABELS — єдине місце, де описано ВСІ поля
  профілю та їхній порядок (handlers/job_profile.py будує з цього майстер
  заповнення). Додано нові поля: level, projects, certifications,
  relocation, industries, dealbreakers, available_from.
- skipped_fields — поля, на які користувач відповів «-» (свідомо порожні),
  щоб бот не питав їх знову у режимі «Доповнити порожні».
- get_profile_status() — скільки полів заповнено, який відсоток і чого
  не вистачає.
- format_profile_for_ai() — готовий текст профілю (усі непорожні поля з
  підписами) для промпту оцінки збігу вакансій. Якщо
  services/jobs_service.py збирає промпт із фіксованого списку полів —
  підключи цю функцію, тоді AI побачить і нові поля.
- should_send_profile_hint() / mark_profile_hint_sent() — не частіше ніж
  раз на PROFILE_HINT_EVERY_DAYS днів нагадати про неповний профіль.

Попередні функції (digest_seen_ids, digest_enabled тощо) — без змін.
"""

from datetime import datetime, timedelta

from database.mongo import job_profiles_col, db_call

DIGEST_SEEN_LIMIT = 500

# Порядок = порядок питань у майстрі заповнення.
PROFILE_FIELDS = (
    "profession",
    "level",
    "experience",
    "skills",
    "projects",
    "education",
    "certifications",
    "languages",
    "desired_salary",
    "location",
    "relocation",
    "work_format",
    "employment_type",
    "industries",
    "dealbreakers",
    "available_from",
    "portfolio_url",
    "linkedin",
    "github",
    "resume_summary",
)

PROFILE_LABELS = {
    "profession": "Професія",
    "level": "Рівень",
    "experience": "Досвід роботи",
    "skills": "Навички",
    "projects": "Проєкти й досягнення",
    "education": "Освіта",
    "certifications": "Курси й сертифікати",
    "languages": "Мови",
    "desired_salary": "Бажана зарплата",
    "location": "Локація",
    "relocation": "Готовність до переїзду",
    "work_format": "Формат роботи",
    "employment_type": "Тип зайнятості",
    "industries": "Бажані сфери",
    "dealbreakers": "Що не підходить",
    "available_from": "Коли можу почати",
    "portfolio_url": "Портфоліо",
    "linkedin": "LinkedIn",
    "github": "GitHub",
    "resume_summary": "Про себе",
}

# Посилання не впливають на збіг — у промпт для AI їх не передаємо.
_AI_SKIP_FIELDS = {"portfolio_url", "linkedin", "github"}

PROFILE_HINT_BELOW_PERCENT = 70
PROFILE_HINT_EVERY_DAYS = 7


def _has_value(value) -> bool:
    return value is not None and bool(str(value).strip())


async def save_profile(uid: int, data: dict) -> None:
    data["uid"] = uid
    data["updated_at"] = datetime.now().isoformat()
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": data}, upsert=True)
    )


async def update_profile_field(uid: int, field: str, value: str) -> None:
    await db_call(
        job_profiles_col.update_one(
            {"uid": uid},
            {"$set": {field: value, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )
    )


async def get_profile(uid: int) -> dict | None:
    return await db_call(job_profiles_col.find_one({"uid": uid}), raise_on_fail=False)


async def has_profile(uid: int) -> bool:
    doc = await get_profile(uid)
    return bool(doc and doc.get("profession"))


# =========================================================
# НОВЕ: повнота профілю
# =========================================================

def get_profile_status(profile: dict | None) -> dict:
    """Поле вважається «готовим», якщо воно заповнене АБО користувач
    свідомо пропустив його через «-» (skipped_fields).
    Повертає {"done", "total", "percent", "missing": [імена полів]}."""
    profile = profile or {}
    skipped = set(profile.get("skipped_fields") or [])
    missing = [
        f for f in PROFILE_FIELDS
        if not _has_value(profile.get(f)) and f not in skipped
    ]
    total = len(PROFILE_FIELDS)
    done = total - len(missing)
    return {
        "done": done,
        "total": total,
        "percent": round(done * 100 / total),
        "missing": missing,
    }


def format_profile_for_ai(profile: dict | None) -> str:
    """Усі непорожні поля профілю у вигляді «- Підпис: значення» —
    для вставки в промпт оцінки збігу вакансій."""
    if not profile:
        return ""
    lines = []
    for field in PROFILE_FIELDS:
        if field in _AI_SKIP_FIELDS:
            continue
        value = profile.get(field)
        if _has_value(value):
            lines.append(f"- {PROFILE_LABELS[field]}: {str(value).strip()}")
    return "\n".join(lines)


def should_send_profile_hint(profile: dict | None) -> bool:
    """True, якщо профіль заповнений менш ніж на PROFILE_HINT_BELOW_PERCENT%
    і нагадування не надсилалось останні PROFILE_HINT_EVERY_DAYS днів."""
    if get_profile_status(profile)["percent"] >= PROFILE_HINT_BELOW_PERCENT:
        return False
    last = (profile or {}).get("profile_hint_date")
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) >= timedelta(days=PROFILE_HINT_EVERY_DAYS)
    except ValueError:
        return True


async def mark_profile_hint_sent(uid: int) -> None:
    await db_call(
        job_profiles_col.update_one(
            {"uid": uid},
            {"$set": {"profile_hint_date": datetime.now().isoformat()}},
            upsert=True,
        ),
        raise_on_fail=False,
    )


# =========================================================
# 🌙 Вечірній підбір вакансій за профілем
# =========================================================

async def get_digest_seen_ids(uid: int) -> set[str]:
    doc = await get_profile(uid)
    return set((doc or {}).get("digest_seen_ids") or [])


async def add_digest_seen_ids(uid: int, new_ids: list[str]) -> None:
    if not new_ids:
        return
    existing = await get_digest_seen_ids(uid)
    existing.update(new_ids)
    # тримаємо лише останні DIGEST_SEEN_LIMIT, щоб документ не ріс безмежно
    trimmed = list(existing)[-DIGEST_SEEN_LIMIT:]
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": {"digest_seen_ids": trimmed}}, upsert=True),
        raise_on_fail=False,
    )


async def is_digest_enabled(uid: int) -> bool:
    doc = await get_profile(uid)
    # за замовчуванням увімкнено — якщо профіль заповнений, юзер явно
    # зацікавлений у підборі вакансій
    return bool((doc or {}).get("digest_enabled", True))


async def set_digest_enabled(uid: int, enabled: bool) -> None:
    await db_call(
        job_profiles_col.update_one({"uid": uid}, {"$set": {"digest_enabled": enabled}}, upsert=True)
    )