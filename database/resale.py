# database/resale.py
from datetime import datetime, timedelta
from bson import ObjectId
import config.settings as cfg
from database.mongo import resale_saved_col, resale_monitors_col, db_call
resale_candidates_col = resale_monitors_col.database['resale_candidates']
resale_settings_col = resale_monitors_col.database['resale_user_settings']
MAX_SHOWN_PER_MONITOR = 1000
MAX_LEARNED_KEYWORDS = 30

def _oid(value):
    try:
        return ObjectId(value)
    except Exception:
        return None

def _empty_stats() -> dict:
    return {'found': 0, 'saved': 0, 'bought': 0, 'sold': 0, 'potential_profit': 0.0, 'actual_profit': 0.0}

async def add_monitor(uid: int, params: dict) -> str:
    now = datetime.now().isoformat()
    location = params.get('location') or ''
    doc = {'uid': uid, 'name': params.get('name') or params.get('category') or params.get('keywords') or 'Автопошук', 'keywords': params.get('keywords') or '', 'category': params.get('category') or '', 'min_price': params.get('min_price'), 'max_price': params.get('max_price'), 'min_resale_price': params.get('min_resale_price'), 'min_profit': params.get('min_profit'), 'min_margin_percent': params.get('min_margin_percent'), 'location': location, 'radius_km': 100 if location else 0, 'condition': params.get('condition'), 'extra_keywords': params.get('extra_keywords') or '', 'exclude_words': params.get('exclude_words') or '', 'domain': 'olx.ua', 'status': 'active', 'shown': [], 'liked_keywords': [], 'disliked_keywords': [], 'blocked_similar': [], 'stats': _empty_stats(), 'created_at': now, 'last_checked_at': None, 'last_run': None}
    result = await db_call(resale_monitors_col.insert_one(doc))
    return str(result.inserted_id)

async def update_monitor(monitor_id, uid: int, fields: dict) -> bool:
    oid = _oid(monitor_id)
    if oid is None:
        return False
    fields = dict(fields)
    if 'location' in fields:
        fields['radius_km'] = 100 if fields['location'] else 0
    result = await db_call(resale_monitors_col.update_one({'_id': oid, 'uid': uid}, {'$set': fields}))
    return result.matched_count > 0

async def get_user_monitors(uid: int) -> list[dict]:
    cursor = resale_monitors_col.find({'uid': uid})
    return await db_call(cursor.to_list(length=200))

async def get_monitor(monitor_id) -> dict | None:
    oid = _oid(monitor_id)
    if oid is None:
        return None
    return await db_call(resale_monitors_col.find_one({'_id': oid}))

async def get_all_active_monitors() -> list[dict]:
    cursor = resale_monitors_col.find({'status': 'active'})
    return await db_call(cursor.to_list(length=2000))

async def set_monitor_status(monitor_id, uid: int, status: str) -> bool:
    oid = _oid(monitor_id)
    if oid is None:
        return False
    result = await db_call(resale_monitors_col.update_one({'_id': oid, 'uid': uid}, {'$set': {'status': status}}))
    return result.matched_count > 0

async def set_all_status(uid: int, status: str) -> int:
    result = await db_call(resale_monitors_col.update_many({'uid': uid}, {'$set': {'status': status}}))
    return result.matched_count

async def delete_monitor(monitor_id, uid: int) -> bool:
    oid = _oid(monitor_id)
    if oid is None:
        return False
    result = await db_call(resale_monitors_col.delete_one({'_id': oid, 'uid': uid}))
    if result.deleted_count > 0:
        await db_call(resale_candidates_col.delete_many({'monitor_id': str(monitor_id)}), raise_on_fail=False)
        return True
    return False

async def set_run_info(monitor_id, info: dict):
    oid = _oid(monitor_id)
    if oid is None:
        return
    now = datetime.now().isoformat()
    await db_call(resale_monitors_col.update_one({'_id': oid}, {'$set': {'last_checked_at': now, 'last_run': {**info, 'at': now}}}), raise_on_fail=False)

async def increment_stat(monitor_id, field: str, amount: float=1):
    oid = _oid(monitor_id)
    if oid is None:
        return
    await db_call(resale_monitors_col.update_one({'_id': oid}, {'$inc': {f'stats.{field}': amount}}), raise_on_fail=False)

def shown_map(monitor: dict) -> dict:
    result: dict = {}
    for e in monitor.get('seen') or []:
        if e.get('url'):
            result[e['url']] = None
    for e in monitor.get('shown') or []:
        if e.get('url'):
            result[e['url']] = e.get('price')
    return result

async def mark_shown(monitor_id, items: list[dict]):
    oid = _oid(monitor_id)
    if oid is None or not items:
        return
    now = datetime.now().isoformat()
    urls = [c['url'] for c in items if c.get('url')]
    entries = [{'url': c['url'], 'price': c.get('price'), 'at': now} for c in items if c.get('url')]
    if urls:
        await db_call(resale_monitors_col.update_one({'_id': oid}, {'$pull': {'shown': {'url': {'$in': urls}}}}), raise_on_fail=False)
        await db_call(resale_monitors_col.update_one({'_id': oid}, {'$push': {'shown': {'$each': entries, '$slice': -MAX_SHOWN_PER_MONITOR}}}), raise_on_fail=False)
    ids = [c['_id'] for c in items if c.get('_id')]
    if ids:
        await db_call(resale_candidates_col.update_many({'_id': {'$in': ids}}, {'$set': {'status': 'shown', 'shown_at': now}}), raise_on_fail=False)

async def add_learned_keyword(monitor_id, keyword: str, positive: bool):
    keyword = (keyword or '').strip().lower()
    if not keyword:
        return
    field = 'liked_keywords' if positive else 'disliked_keywords'
    monitor = await get_monitor(monitor_id)
    if not monitor:
        return
    lst = monitor.get(field) or []
    if keyword not in lst:
        lst.append(keyword)
    lst = lst[-MAX_LEARNED_KEYWORDS:]
    await db_call(resale_monitors_col.update_one({'_id': monitor['_id']}, {'$set': {field: lst}}), raise_on_fail=False)

async def add_blocked_similar(monitor_id, keyword: str):
    keyword = (keyword or '').strip().lower()
    if not keyword:
        return
    monitor = await get_monitor(monitor_id)
    if not monitor:
        return
    lst = monitor.get('blocked_similar') or []
    if keyword not in lst:
        lst.append(keyword)
    await db_call(resale_monitors_col.update_one({'_id': monitor['_id']}, {'$set': {'blocked_similar': lst}}), raise_on_fail=False)

async def upsert_candidate(monitor_id, uid: int, url: str, fields: dict):
    now = datetime.now().isoformat()
    mid = str(monitor_id)
    set_fields = {k: v for k, v in fields.items() if k not in ('monitor_id', 'url', 'uid')}
    set_fields['updated_at'] = now
    defaults = {'uid': uid, 'found_at': now, 'day': now[:10], 'status': 'new'}
    on_insert = {k: v for k, v in defaults.items() if k not in set_fields}
    await db_call(resale_candidates_col.update_one({'monitor_id': mid, 'url': url}, {'$set': set_fields, '$setOnInsert': on_insert}, upsert=True), raise_on_fail=False)

async def get_known_candidates(monitor_id) -> dict:
    cursor = resale_candidates_col.find({'monitor_id': str(monitor_id)}, {'url': 1, 'status': 1, 'price': 1})
    docs = await db_call(cursor.to_list(length=5000), raise_on_fail=False) or []
    return {d['url']: d for d in docs if d.get('url')}

async def get_candidate(cid) -> dict | None:
    oid = _oid(cid)
    if oid is None:
        return None
    return await db_call(resale_candidates_col.find_one({'_id': oid}), raise_on_fail=False)

async def update_candidate(cid, fields: dict):
    oid = _oid(cid)
    if oid is None:
        return
    await db_call(resale_candidates_col.update_one({'_id': oid}, {'$set': {**fields, 'updated_at': datetime.now().isoformat()}}), raise_on_fail=False)

async def get_active_candidates(monitor_id, cutoff_iso: str, limit: int=30) -> list[dict]:
    cursor = resale_candidates_col.find({'monitor_id': str(monitor_id), 'status': 'new', 'found_at': {'$gte': cutoff_iso}}).sort('score', -1).limit(limit)
    return await db_call(cursor.to_list(length=limit), raise_on_fail=False) or []

async def get_report_candidates(monitor_id, cutoff_iso: str, limit: int) -> list[dict]:
    return await get_active_candidates(monitor_id, cutoff_iso, limit)

async def day_stats(monitor_id, day: str) -> dict:
    mid = str(monitor_id)
    found = await db_call(resale_candidates_col.count_documents({'monitor_id': mid, 'day': day}), raise_on_fail=False) or 0
    sel_q = {'monitor_id': mid, 'day': day, 'status': {'$in': ['new', 'shown']}}
    selected = await db_call(resale_candidates_col.count_documents(sel_q), raise_on_fail=False) or 0
    best = await db_call(resale_candidates_col.find_one(sel_q, sort=[('profit', -1)]), raise_on_fail=False)
    return {'found': found, 'selected': selected, 'best_profit': (best or {}).get('profit')}

async def get_history(uid: int, limit: int=10) -> list[dict]:
    cursor = resale_candidates_col.find({'uid': uid, 'status': 'shown'}).sort('shown_at', -1).limit(limit)
    return await db_call(cursor.to_list(length=limit), raise_on_fail=False) or []

async def cleanup_candidates(days: int=14):
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    await db_call(resale_candidates_col.delete_many({'day': {'$lt': cutoff}}), raise_on_fail=False)

def _default_user_settings() -> dict:
    return {'delivery_cost': float(getattr(cfg, 'RESALE_DELIVERY_COST', 80)), 'packing_cost': float(getattr(cfg, 'RESALE_PACKING_COST', 20)), 'commission_percent': float(getattr(cfg, 'RESALE_COMMISSION_PERCENT', 0))}

async def get_user_settings(uid: int) -> dict:
    base = _default_user_settings()
    doc = await db_call(resale_settings_col.find_one({'uid': uid}), raise_on_fail=False) or {}
    for key in base:
        if isinstance(doc.get(key), (int, float)):
            base[key] = float(doc[key])
    return base

async def set_user_setting(uid: int, key: str, value: float):
    if key not in _default_user_settings():
        return
    await db_call(resale_settings_col.update_one({'uid': uid}, {'$set': {key: float(value)}}, upsert=True), raise_on_fail=False)

async def save_opportunity(uid: int, monitor_id, opp: dict) -> str:
    listing = opp.get('listing') or {}
    analysis = opp.get('analysis') or {}
    doc = {'uid': uid, 'monitor_id': str(monitor_id) if monitor_id else None, 'url': listing.get('url'), 'title': analysis.get('item_name') or listing.get('title'), 'purchase_price': listing.get('price'), 'currency': listing.get('currency', 'UAH'), 'score': opp.get('score'), 'profit': opp.get('profit'), 'margin': opp.get('margin'), 'risk': (analysis.get('risks') or {}).get('level'), 'status': 'interest', 'saved_at': datetime.now().isoformat()}
    result = await db_call(resale_saved_col.insert_one(doc))
    return str(result.inserted_id)

async def get_saved(uid: int) -> list[dict]:
    cursor = resale_saved_col.find({'uid': uid})
    return await db_call(cursor.to_list(length=200))

async def get_saved_item(item_id) -> dict | None:
    oid = _oid(item_id)
    if oid is None:
        return None
    return await db_call(resale_saved_col.find_one({'_id': oid}))

async def set_saved_status(item_id, uid: int, status: str) -> bool:
    oid = _oid(item_id)
    if oid is None:
        return False
    result = await db_call(resale_saved_col.update_one({'_id': oid, 'uid': uid}, {'$set': {'status': status, 'status_at': datetime.now().isoformat()}}))
    return result.matched_count > 0

async def delete_saved(item_id, uid: int) -> bool:
    oid = _oid(item_id)
    if oid is None:
        return False
    result = await db_call(resale_saved_col.delete_one({'_id': oid, 'uid': uid}))
    return result.deleted_count > 0