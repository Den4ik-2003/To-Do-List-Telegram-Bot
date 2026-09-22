# database/job_profile.py
from datetime import datetime, timedelta
from database.mongo import job_profiles_col, db_call
DIGEST_SEEN_LIMIT = 500
PROFILE_FIELDS = ('profession', 'level', 'experience', 'skills', 'projects', 'education', 'certifications', 'languages', 'desired_salary', 'location', 'relocation', 'work_format', 'employment_type', 'industries', 'dealbreakers', 'available_from', 'portfolio_url', 'linkedin', 'github', 'resume_summary')
PROFILE_LABELS = {'profession': 'Професія', 'level': 'Рівень', 'experience': 'Досвід роботи', 'skills': 'Навички', 'projects': 'Проєкти й досягнення', 'education': 'Освіта', 'certifications': 'Курси й сертифікати', 'languages': 'Мови', 'desired_salary': 'Бажана зарплата', 'location': 'Локація', 'relocation': 'Готовність до переїзду', 'work_format': 'Формат роботи', 'employment_type': 'Тип зайнятості', 'industries': 'Бажані сфери', 'dealbreakers': 'Що не підходить', 'available_from': 'Коли можу почати', 'portfolio_url': 'Портфоліо', 'linkedin': 'LinkedIn', 'github': 'GitHub', 'resume_summary': 'Про себе'}
_AI_SKIP_FIELDS = {'portfolio_url', 'linkedin', 'github'}
PROFILE_HINT_BELOW_PERCENT = 70
PROFILE_HINT_EVERY_DAYS = 7

def _has_value(value) -> bool:
    return value is not None and bool(str(value).strip())

async def save_profile(uid: int, data: dict) -> None:
    data['uid'] = uid
    data['updated_at'] = datetime.now().isoformat()
    await db_call(job_profiles_col.update_one({'uid': uid}, {'$set': data}, upsert=True))

async def update_profile_field(uid: int, field: str, value: str) -> None:
    await db_call(job_profiles_col.update_one({'uid': uid}, {'$set': {field: value, 'updated_at': datetime.now().isoformat()}}, upsert=True))

async def get_profile(uid: int) -> dict | None:
    return await db_call(job_profiles_col.find_one({'uid': uid}), raise_on_fail=False)

async def has_profile(uid: int) -> bool:
    doc = await get_profile(uid)
    return bool(doc and doc.get('profession'))

def get_profile_status(profile: dict | None) -> dict:
    profile = profile or {}
    skipped = set(profile.get('skipped_fields') or [])
    missing = [f for f in PROFILE_FIELDS if not _has_value(profile.get(f)) and f not in skipped]
    total = len(PROFILE_FIELDS)
    done = total - len(missing)
    return {'done': done, 'total': total, 'percent': round(done * 100 / total), 'missing': missing}

def format_profile_for_ai(profile: dict | None) -> str:
    if not profile:
        return ''
    lines = []
    for field in PROFILE_FIELDS:
        if field in _AI_SKIP_FIELDS:
            continue
        value = profile.get(field)
        if _has_value(value):
            lines.append(f'- {PROFILE_LABELS[field]}: {str(value).strip()}')
    return '\n'.join(lines)

def should_send_profile_hint(profile: dict | None) -> bool:
    if get_profile_status(profile)['percent'] >= PROFILE_HINT_BELOW_PERCENT:
        return False
    last = (profile or {}).get('profile_hint_date')
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) >= timedelta(days=PROFILE_HINT_EVERY_DAYS)
    except ValueError:
        return True

async def mark_profile_hint_sent(uid: int) -> None:
    await db_call(job_profiles_col.update_one({'uid': uid}, {'$set': {'profile_hint_date': datetime.now().isoformat()}}, upsert=True), raise_on_fail=False)

async def get_digest_seen_ids(uid: int) -> set[str]:
    doc = await get_profile(uid)
    return set((doc or {}).get('digest_seen_ids') or [])

async def add_digest_seen_ids(uid: int, new_ids: list[str]) -> None:
    if not new_ids:
        return
    existing = await get_digest_seen_ids(uid)
    existing.update(new_ids)
    trimmed = list(existing)[-DIGEST_SEEN_LIMIT:]
    await db_call(job_profiles_col.update_one({'uid': uid}, {'$set': {'digest_seen_ids': trimmed}}, upsert=True), raise_on_fail=False)

async def is_digest_enabled(uid: int) -> bool:
    doc = await get_profile(uid)
    return bool((doc or {}).get('digest_enabled', True))

async def set_digest_enabled(uid: int, enabled: bool) -> None:
    await db_call(job_profiles_col.update_one({'uid': uid}, {'$set': {'digest_enabled': enabled}}, upsert=True))