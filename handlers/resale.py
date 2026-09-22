# handlers/resale.py
import asyncio
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from database import resale as resale_db
from services import resale_service
from services import resale_engine
from services.resale_service import esc
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth
logger = logging.getLogger('tasks_bot')
router = Router(name='resale')
CANCEL_TEXT = '❌ Скасувати'
NONE_WORDS = ('немає', 'нема', '-')
_CONDITION_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='Будь-який')], [KeyboardButton(text='Новий'), KeyboardButton(text='Вживаний')], [KeyboardButton(text=CANCEL_TEXT)]], resize_keyboard=True)
_CONDITION_MAP = {'новий': 'new', 'вживаний': 'used', 'будь-який': None}
_CONDITION_LABEL = {'new': 'новий', 'used': 'вживаний', None: 'будь-який'}
_STEPS = [('name', '🏷 Назва автопошуку (напр. `Кросівки Nike`):', 'text_req'), ('keywords', '🔑 Ключові слова для пошуку на OLX (напр. `nike air max`), або «немає»:', 'text'), ('category', '📂 Категорія (напр. `кросівки`), або «немає»:', 'text'), ('min_price', '💰 Мінімальна ціна купівлі (грн), або «немає»:', 'num'), ('max_price', '💰 Максимальна ціна купівлі (грн), або «немає»:', 'num'), ('min_resale_price', '🔄 Бажана мінімальна ціна перепродажу (грн), або «немає»:', 'num'), ('min_profit', '💵 Мінімальний очікуваний прибуток (грн), або «немає»:', 'num'), ('min_margin_percent', '📈 Мінімальна маржа у %, або «немає»:', 'num'), ('location', '📍 Місто / область, або «немає» — вся Україна:', 'text'), ('condition', '🏷 Стан товару:', 'cond'), ('extra_keywords', '➕ Додаткові ключові слова (бажані в назві/описі, через кому), або «немає»:', 'text'), ('exclude_words', '🚫 Слова-виключення (через кому, напр. `чохол, запчастини`), або «немає»:', 'text')]
_SETTING_LABELS = {'delivery_cost': ('🚚 Доставка', 'грн'), 'packing_cost': ('📦 Пакування', 'грн'), 'commission_percent': ('💳 Комісія з продажу', '%')}

class ResaleMonitor(StatesGroup):
    step = State()

class ResaleSettings(StatesGroup):
    value = State()
_tasks: set = set()

def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

def _is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT

def _parse_number(text: str):
    raw = (text or '').strip().lower()
    if raw in NONE_WORDS:
        return None
    try:
        value = float(raw.replace(' ', '').replace(',', '.'))
    except ValueError:
        return 'invalid'
    return 'invalid' if value < 0 else value

def _show(value) -> str:
    if value in (None, ''):
        return 'немає'
    if isinstance(value, float):
        return f'{value:.0f}'
    return str(value)

def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')

def _ikb_resale_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='➕ Створити автопошук', callback_data='rsm_new')], [InlineKeyboardButton(text='🔎 Мої автопошуки', callback_data='rsm_list')], [InlineKeyboardButton(text='⏸ Поставити на паузу', callback_data='rsm_pause_all'), InlineKeyboardButton(text='▶️ Запустити', callback_data='rsm_resume_all')], [InlineKeyboardButton(text='⚙️ Налаштування', callback_data='rsm_settings'), InlineKeyboardButton(text='📊 Історія результатів', callback_data='rsm_history')], [InlineKeyboardButton(text='⭐ Збережені можливості', callback_data='rsm_saved'), InlineKeyboardButton(text='📈 Статистика', callback_data='rsm_stats')]])

def _ikb_monitor(m: dict) -> InlineKeyboardMarkup:
    mid = str(m['_id'])
    active = m.get('status', 'active') == 'active'
    toggle_text = '⏸ Пауза' if active else '▶️ Запустити'
    toggle_cb = f'rsm_pause:{mid}' if active else f'rsm_resume:{mid}'
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✏️ Редагувати', callback_data=f'rsm_edit:{mid}'), InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)], [InlineKeyboardButton(text='🔍 Знайти зараз', callback_data=f'rsm_run:{mid}'), InlineKeyboardButton(text='📊 Статистика', callback_data=f'rsm_mstats:{mid}')], [InlineKeyboardButton(text='🗑 Видалити', callback_data=f'rsm_del:{mid}')]])

def _ikb_settings() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'{label} — змінити', callback_data=f'rsm_set:{key}')] for key, (label, _unit) in _SETTING_LABELS.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _ikb_saved_item(item_id: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == 'interest':
        rows.append([InlineKeyboardButton(text='🛒 Купив', callback_data=f'rss_status:{item_id}:bought')])
    if status == 'bought':
        rows.append([InlineKeyboardButton(text='💰 Перепродав', callback_data=f'rss_status:{item_id}:sold')])
    if status in ('interest', 'bought'):
        rows.append([InlineKeyboardButton(text='❌ Відмовився', callback_data=f'rss_status:{item_id}:rejected')])
    rows.append([InlineKeyboardButton(text='🗑 Видалити', callback_data=f'rss_del:{item_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _fmt_saved_line(s: dict) -> str:
    status_labels = {'interest': '⭐ Цікаво', 'bought': '🛒 Куплено', 'sold': '💰 Перепродано', 'rejected': '❌ Відмовився'}
    return f'📦 *{esc(s.get('title') or '—')}*\n💰 Купівля: {s.get('purchase_price', '?')} {s.get('currency', '')}\n📈 Оцінка: {s.get('score', '?')}/100, маржа ~{s.get('margin', '?')}%\n📌 Статус: {status_labels.get(s.get('status'), s.get('status'))}\n🔗 {s.get('url', '')}'

async def _card(m: dict) -> str:
    day = await resale_db.day_stats(m['_id'], _today())
    return resale_service.format_monitor_card(m, day)

async def _own_monitor(mid: str, uid: int) -> dict | None:
    monitor = await resale_db.get_monitor(mid)
    if not monitor or monitor.get('uid') != uid:
        return None
    return monitor

@router.message(F.text.in_(['🔎 Знайти перепродаж', '📈 Статистика перепродажу']))
async def resale_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if msg.text == '📈 Статистика перепродажу':
        return await _show_stats(msg)
    (mh, mm), (eh, em) = resale_service.schedule_times()
    await msg.answer(f'🔎 *Знайти перепродаж — AI-автопошуки OLX*\n\nСтвори автопошуки, і я сам шукатиму товари, які вигідно купити та перепродати.\n🕒 Кожен автопошук працює двічі на добу: збір ~{mh:02d}:{mm:02d}, звіт ~{eh:02d}:{em:02d}.\n📌 Я показую мало, але реально цікаві варіанти: до 3 на кожен автопошук.', reply_markup=_ikb_resale_menu())

async def _ask(msg: Message, state: FSMContext, idx: int):
    key, prompt, kind = _STEPS[idx]
    fd = await state.get_data()
    orig = fd.get('orig') or {}
    hint = ''
    if key in orig:
        cur = _CONDITION_LABEL.get(orig[key]) if kind == 'cond' else _show(orig[key])
        hint = f'\n_Зараз: {esc(cur)}. Введи «=», щоб лишити._'
    await state.update_data(idx=idx)
    kb = _CONDITION_KB if kind == 'cond' else kb_cancel()
    await msg.answer(f'*Крок {idx + 1}/{len(_STEPS)}*\n{prompt}{hint}', reply_markup=kb)

@router.callback_query(F.data == 'rsm_new')
async def resale_new_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ResaleMonitor.step)
    await state.update_data(idx=0, values={}, edit_id=None, orig={})
    await _ask(cb.message, state, 0)

@router.callback_query(F.data.startswith('rsm_edit:'))
async def resale_edit_cb(cb: CallbackQuery, state: FSMContext):
    mid = cb.data.split(':', 1)[1]
    monitor = await _own_monitor(mid, cb.from_user.id)
    await cb.answer()
    if not monitor:
        return await cb.message.answer('⚠️ Автопошук не знайдено.')
    orig = {key: monitor.get(key) for key, _, _ in _STEPS}
    orig['name'] = orig.get('name') or monitor.get('category')
    await state.clear()
    await state.set_state(ResaleMonitor.step)
    await state.update_data(idx=0, values={}, edit_id=mid, orig=orig)
    await cb.message.answer('✏️ *Редагування автопошуку.* На кожному кроці введи нове значення або «=», щоб лишити поточне.')
    await _ask(cb.message, state, 0)

@router.message(ResaleMonitor.step)
async def rm_step(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer('Скасовано.', reply_markup=kb_main())
    text = (msg.text or '').strip()
    fd = await state.get_data()
    idx = fd.get('idx', 0)
    key, _prompt, kind = _STEPS[idx]
    orig = fd.get('orig') or {}
    if text == '=' and key in orig:
        value = orig[key]
    elif kind == 'num':
        value = _parse_number(text)
        if value == 'invalid':
            return await msg.answer('⚠️ Введи додатне число або «немає»:')
    elif kind == 'cond':
        low = text.lower()
        if low not in _CONDITION_MAP:
            return await msg.answer('⚠️ Обери варіант на клавіатурі:', reply_markup=_CONDITION_KB)
        value = _CONDITION_MAP[low]
    elif kind == 'text_req':
        if not text or text.lower() in NONE_WORDS:
            return await msg.answer("⚠️ Назва обов'язкова. Введи назву автопошуку:")
        value = text
    else:
        value = '' if text.lower() in NONE_WORDS else text
    values = dict(fd.get('values') or {})
    values[key] = value
    await state.update_data(values=values)
    if idx + 1 < len(_STEPS):
        return await _ask(msg, state, idx + 1)
    await _finish(msg, state, values, fd.get('edit_id'))

async def _finish(msg: Message, state: FSMContext, values: dict, edit_id: str | None):
    uid = msg.from_user.id
    await state.clear()
    if not (values.get('keywords') or values.get('category')):
        values['keywords'] = values.get('name') or ''
    if edit_id:
        await resale_db.update_monitor(edit_id, uid, values)
        monitor_id, verb = (edit_id, 'оновлено')
    else:
        monitor_id, verb = (await resale_db.add_monitor(uid, values), 'створено')
    (_, _), (eh, em) = resale_service.schedule_times()
    await msg.answer(f'✅ Автопошук «{esc(values.get('name'))}» {verb}!\n🕒 Далі він працює автоматично двічі на добу, звіт приходить ~{eh:02d}:{em:02d}.\n⏳ Запускаю перший збір у фоні, скажу, коли він завершиться.', reply_markup=kb_main())
    monitor = await resale_db.get_monitor(monitor_id)
    if monitor:
        _spawn(_first_collect(msg.bot, monitor))

async def _first_collect(bot, monitor: dict):
    mid = str(monitor['_id'])
    if not resale_service.try_acquire(mid):
        return
    try:
        res = await resale_service.scan_monitor(monitor)
        await bot.send_message(monitor['uid'], resale_service.format_scan_summary(res, first=True))
    except Exception:
        logger.exception('resale: перший збір упав, monitor=%s', mid)
        try:
            await bot.send_message(monitor['uid'], '⚠️ Перший збір не вдався, спробую автоматично в наступний запуск.')
        except Exception:
            pass
    finally:
        resale_service.release(mid)

@router.callback_query(F.data == 'rsm_list')
async def resale_list_cb(cb: CallbackQuery):
    await cb.answer()
    monitors = await resale_db.get_user_monitors(cb.from_user.id)
    if not monitors:
        return await cb.message.answer('📭 У тебе ще немає автопошуків. Тисни «➕ Створити автопошук».')
    for m in monitors:
        await cb.message.answer(await _card(m), reply_markup=_ikb_monitor(m))

async def _refresh_card(cb: CallbackQuery, mid: str):
    monitor = await resale_db.get_monitor(mid)
    if not monitor:
        return
    try:
        await cb.message.edit_text(await _card(monitor), reply_markup=_ikb_monitor(monitor))
    except Exception:
        pass

@router.callback_query(F.data.startswith('rsm_pause:'))
async def resale_pause_cb(cb: CallbackQuery):
    mid = cb.data.split(':', 1)[1]
    ok = await resale_db.set_monitor_status(mid, cb.from_user.id, 'paused')
    await cb.answer('Призупинено ⏸' if ok else 'Не знайдено', show_alert=not ok)
    if ok:
        await _refresh_card(cb, mid)

@router.callback_query(F.data.startswith('rsm_resume:'))
async def resale_resume_cb(cb: CallbackQuery):
    mid = cb.data.split(':', 1)[1]
    ok = await resale_db.set_monitor_status(mid, cb.from_user.id, 'active')
    await cb.answer('Запущено ▶️' if ok else 'Не знайдено', show_alert=not ok)
    if ok:
        await _refresh_card(cb, mid)

@router.callback_query(F.data == 'rsm_pause_all')
async def resale_pause_all_cb(cb: CallbackQuery):
    n = await resale_db.set_all_status(cb.from_user.id, 'paused')
    await cb.answer(f'На паузі: {n} ⏸' if n else 'Автопошуків немає', show_alert=not n)

@router.callback_query(F.data == 'rsm_resume_all')
async def resale_resume_all_cb(cb: CallbackQuery):
    n = await resale_db.set_all_status(cb.from_user.id, 'active')
    await cb.answer(f'Запущено: {n} ▶️' if n else 'Автопошуків немає', show_alert=not n)

@router.callback_query(F.data.startswith('rsm_del:'))
async def resale_delete_cb(cb: CallbackQuery):
    mid = cb.data.split(':', 1)[1]
    ok = await resale_db.delete_monitor(mid, cb.from_user.id)
    await cb.answer('Видалено ✅' if ok else 'Не знайдено', show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text('🗑 Автопошук видалено.')
        except Exception:
            pass

@router.callback_query(F.data.startswith('rsm_mstats:'))
async def resale_monitor_stats_cb(cb: CallbackQuery):
    mid = cb.data.split(':', 1)[1]
    monitor = await _own_monitor(mid, cb.from_user.id)
    await cb.answer()
    if not monitor:
        return await cb.message.answer('⚠️ Автопошук не знайдено.')
    stats = monitor.get('stats') or {}
    day = await resale_db.day_stats(mid, _today())
    last = monitor.get('last_run') or {}
    await cb.message.answer(f'📊 *Статистика «{esc(monitor.get('name') or monitor.get('category'))}»*\n\n📅 Сьогодні: проаналізовано {day['found']}, відібрано {day['selected']}\n🕒 Останній запуск: переглянуто {last.get('scanned', 0)}, проаналізовано {last.get('analyzed', 0)}, відібрано {last.get('selected', 0)}\n\n🔎 Показано у звітах всього: {stats.get('found', 0)}\n⭐ Збережено: {stats.get('saved', 0)}\n🛒 Куплено: {stats.get('bought', 0)}\n💰 Перепродано: {stats.get('sold', 0)}')

@router.callback_query(F.data.startswith('rsm_run:'))
async def resale_run_now_cb(cb: CallbackQuery):
    mid = cb.data.split(':', 1)[1]
    monitor = await _own_monitor(mid, cb.from_user.id)
    if not monitor:
        return await cb.answer('Не знайдено', show_alert=True)
    if not resale_service.try_acquire(mid):
        return await cb.answer('Цей автопошук уже виконується ⏳', show_alert=True)
    await cb.answer('Запускаю пошук…')
    await cb.message.answer('⏳ Шукаю й оцінюю оголошення. Це може зайняти кілька хвилин, результат надішлю сюди.')
    _spawn(_run_now(cb.bot, monitor))

async def _run_now(bot, monitor: dict):
    mid = str(monitor['_id'])
    try:
        try:
            await resale_service.recheck_candidates(monitor)
        except Exception:
            logger.exception('resale: recheck (ручний) упав, monitor=%s', mid)
        res = await resale_service.scan_monitor(monitor)
        await resale_service.send_report(bot, monitor, error=res.get('error'))
    except Exception:
        logger.exception('resale: ручний пошук упав, monitor=%s', mid)
        try:
            await bot.send_message(monitor['uid'], '⚠️ Пошук не вдався, спробуй пізніше.')
        except Exception:
            pass
    finally:
        resale_service.release(mid)

async def _settings_text(uid: int) -> str:
    us = await resale_db.get_user_settings(uid)
    lines = ['⚙️ *Налаштування розрахунку прибутку*', '', 'Ці витрати віднімаються від очікуваного прибутку кожного варіанта:', '']
    for key, (label, unit) in _SETTING_LABELS.items():
        lines.append(f'{label}: {us[key]:.0f} {unit}')
    return '\n'.join(lines)

@router.callback_query(F.data == 'rsm_settings')
async def resale_settings_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(await _settings_text(cb.from_user.id), reply_markup=_ikb_settings())

@router.callback_query(F.data.startswith('rsm_set:'))
async def resale_setting_edit_cb(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(':', 1)[1]
    if key not in _SETTING_LABELS:
        return await cb.answer('Невідомий параметр', show_alert=True)
    label, unit = _SETTING_LABELS[key]
    await cb.answer()
    await state.set_state(ResaleSettings.value)
    await state.update_data(setting_key=key)
    await cb.message.answer(f'{label}: введи нове значення ({unit}):', reply_markup=kb_cancel())

@router.message(ResaleSettings.value)
async def resale_setting_value(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer('Скасовано.', reply_markup=kb_main())
    fd = await state.get_data()
    key = fd.get('setting_key')
    val = _parse_number(msg.text)
    limit = 100 if key == 'commission_percent' else 100000
    if val in (None, 'invalid') or val > limit:
        return await msg.answer(f'⚠️ Введи число від 0 до {limit}:')
    await resale_db.set_user_setting(msg.from_user.id, key, val)
    await state.clear()
    await msg.answer('✅ Збережено.', reply_markup=kb_main())
    await msg.answer(await _settings_text(msg.from_user.id), reply_markup=_ikb_settings())

@router.callback_query(F.data == 'rsm_history')
async def resale_history_cb(cb: CallbackQuery):
    await cb.answer()
    items = await resale_db.get_history(cb.from_user.id, 10)
    if not items:
        return await cb.message.answer('📭 Історія порожня: звітів із результатами ще не було.')
    lines = ['📊 *Історія результатів* (останні показані варіанти)', '']
    for c in items:
        when = ''
        if c.get('shown_at'):
            try:
                when = datetime.fromisoformat(c['shown_at']).strftime('%d.%m')
            except ValueError:
                pass
        lines.append(f'• {esc(c.get('title') or '—')[:60]} — {resale_service._money(c.get('price'))} грн → прибуток ~{resale_service._money(c.get('profit'))} грн {when}')
    await cb.message.answer('\n'.join(lines))

async def _show_saved(target, uid: int):
    saved = await resale_db.get_saved(uid)
    if not saved:
        return await target.answer('📭 Немає збережених можливостей.')
    for s in saved:
        await target.answer(_fmt_saved_line(s), reply_markup=_ikb_saved_item(str(s['_id']), s.get('status', 'interest')))

@router.callback_query(F.data == 'rsm_saved')
async def resale_saved_cb(cb: CallbackQuery):
    await cb.answer()
    await _show_saved(cb.message, cb.from_user.id)

@router.message(F.text == '⭐ Збережені можливості')
async def resale_saved_msg(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _show_saved(msg, msg.from_user.id)
    await msg.answer('🏠 Головне меню:', reply_markup=kb_main())

@router.callback_query(F.data.startswith('rss_status:'))
async def resale_saved_status_cb(cb: CallbackQuery):
    _, item_id, status = cb.data.split(':')
    ok = await resale_db.set_saved_status(item_id, cb.from_user.id, status)
    await cb.answer('Оновлено ✅' if ok else 'Не знайдено', show_alert=not ok)
    if ok:
        item = await resale_db.get_saved_item(item_id)
        if item and item.get('monitor_id'):
            if status == 'bought':
                await resale_db.increment_stat(item['monitor_id'], 'bought')
            elif status == 'sold':
                await resale_db.increment_stat(item['monitor_id'], 'sold')
                await resale_db.increment_stat(item['monitor_id'], 'actual_profit', item.get('profit') or 0)
        try:
            await cb.message.edit_reply_markup(reply_markup=_ikb_saved_item(item_id, status))
        except Exception:
            pass

@router.callback_query(F.data.startswith('rss_del:'))
async def resale_saved_delete_cb(cb: CallbackQuery):
    item_id = cb.data.split(':', 1)[1]
    ok = await resale_db.delete_saved(item_id, cb.from_user.id)
    await cb.answer('Видалено ✅' if ok else 'Не знайдено', show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text('🗑 Видалено зі збережених.')
        except Exception:
            pass

async def _show_stats(target: Message):
    uid = target.from_user.id
    monitors = await resale_db.get_user_monitors(uid)
    saved = await resale_db.get_saved(uid)
    await target.answer(resale_service.build_statistics_text(monitors, saved), reply_markup=kb_main())

@router.callback_query(F.data == 'rsm_stats')
async def resale_stats_cb(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    monitors = await resale_db.get_user_monitors(uid)
    saved = await resale_db.get_saved(uid)
    await cb.message.answer(resale_service.build_statistics_text(monitors, saved))

async def _own_candidate(cid: str, uid: int) -> dict | None:
    c = await resale_db.get_candidate(cid)
    if not c or c.get('uid') != uid:
        return None
    return c

def _learn_keyword(c: dict) -> str | None:
    a = c.get('analysis') or {}
    return a.get('item_brand') or a.get('item_name')

@router.callback_query(F.data.startswith('rso_analyze:'))
async def resale_opp_analyze_cb(cb: CallbackQuery):
    c = await _own_candidate(cb.data.split(':', 1)[1], cb.from_user.id)
    await cb.answer()
    if not c or not c.get('analysis'):
        return await cb.message.answer('⚠️ Ця знахідка вже застаріла.')
    opp = resale_service.opp_from_candidate(c)
    await cb.message.answer(resale_engine.format_analysis(opp['listing'], opp['analysis'], cached=True))

@router.callback_query(F.data.startswith('rso_save:'))
async def resale_opp_save_cb(cb: CallbackQuery):
    c = await _own_candidate(cb.data.split(':', 1)[1], cb.from_user.id)
    if not c:
        return await cb.answer('Застаріло', show_alert=True)
    if c.get('saved'):
        return await cb.answer('Уже збережено ⭐')
    opp = resale_service.opp_from_candidate(c)
    await resale_db.save_opportunity(cb.from_user.id, c.get('monitor_id'), opp)
    await resale_db.update_candidate(c['_id'], {'saved': True})
    await cb.answer('Збережено ⭐')
    if c.get('monitor_id'):
        await resale_db.increment_stat(c['monitor_id'], 'saved')
        await resale_db.increment_stat(c['monitor_id'], 'potential_profit', c.get('profit') or 0)
        await resale_db.add_learned_keyword(c['monitor_id'], _learn_keyword(c), positive=True)

@router.callback_query(F.data.startswith('rso_skip:'))
async def resale_opp_skip_cb(cb: CallbackQuery):
    c = await _own_candidate(cb.data.split(':', 1)[1], cb.from_user.id)
    await cb.answer('Ок, врахую 👍')
    if not c:
        return
    await resale_db.update_candidate(c['_id'], {'status': 'dismissed'})
    if c.get('monitor_id'):
        await resale_db.add_learned_keyword(c['monitor_id'], _learn_keyword(c), positive=False)

@router.callback_query(F.data.startswith('rso_block:'))
async def resale_opp_block_cb(cb: CallbackQuery):
    c = await _own_candidate(cb.data.split(':', 1)[1], cb.from_user.id)
    await cb.answer('Більше не шукатиму подібне 🔕')
    if not c:
        return
    await resale_db.update_candidate(c['_id'], {'status': 'dismissed'})
    if c.get('monitor_id'):
        await resale_db.add_blocked_similar(c['monitor_id'], _learn_keyword(c))