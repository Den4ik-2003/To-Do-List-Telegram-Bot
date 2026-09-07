"""
handlers/resale.py

Фіча "🔥 Знайти перепродаж" — постійний AI-моніторинг OLX (повна
переробка за ТЗ). AI-аналіз і пошук на OLX не дублюються — вони цілком
у services/resale_service.py + services/olx_scanner.py + resale_engine.py.
Тут — тільки UX: майстер створення моніторингу, керування, збережені
можливості, статистика, кнопки під сповіщенням.
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

from config.constants import AI_ERROR_TEXT
from database import resale as resale_db
from services import resale_service
from services import resale_engine
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="resale")

CANCEL_TEXT = "❌ Скасувати"
NONE_WORDS = ("немає", "нема", "-")

_CONDITION_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Будь-який")],
        [KeyboardButton(text="Новий"), KeyboardButton(text="Вживаний")],
        [KeyboardButton(text=CANCEL_TEXT)],
    ],
    resize_keyboard=True,
)
_CONDITION_MAP = {"новий": "new", "вживаний": "used", "будь-який": None}

_FREQ_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏱ 30 хв"), KeyboardButton(text="⏱ 1 год")],
        [KeyboardButton(text="⏱ 3 год"), KeyboardButton(text="⏱ 6 год")],
        [KeyboardButton(text=CANCEL_TEXT)],
    ],
    resize_keyboard=True,
)
_FREQ_MAP = {"⏱ 30 хв": 30, "⏱ 1 год": 60, "⏱ 3 год": 180, "⏱ 6 год": 360}


class ResaleMonitor(StatesGroup):
    category = State()
    keywords = State()
    location = State()
    min_price = State()
    max_price = State()
    min_profit = State()
    min_margin = State()
    condition = State()
    brand_model = State()
    frequency = State()


def _is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


def _parse_number(text: str) -> float | None:
    raw = (text or "").strip().lower()
    if raw in NONE_WORDS:
        return None
    try:
        return float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        return "invalid"  # відрізняємо "не число" від "користувач сказав немає"


def _ikb_resale_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новий моніторинг", callback_data="rsm_new")],
        [InlineKeyboardButton(text="⚙️ Мої моніторинги", callback_data="rsm_list")],
        [InlineKeyboardButton(text="⭐ Збережені можливості", callback_data="rsm_saved")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="rsm_stats")],
    ])


def _ikb_monitor(m: dict) -> InlineKeyboardMarkup:
    mid = str(m["_id"])
    active = m.get("status", "active") == "active"
    toggle_text = "⏸️ Пауза" if active else "▶️ Запустити"
    toggle_cb = f"rsm_pause:{mid}" if active else f"rsm_resume:{mid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"rsm_edit:{mid}"),
         InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"rsm_mstats:{mid}"),
         InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"rsm_del:{mid}")],
    ])


def _fmt_monitor_line(m: dict) -> str:
    status = "🟢 активний" if m.get("status", "active") == "active" else "⏸️ на паузі"
    price_text = f"до {m['max_price']:.0f} грн" if m.get("max_price") else "без обмеження ціни"
    loc = m.get("location") or "вся Україна"
    return (
        f"🔥 *{m.get('category') or '—'}*\n"
        f"{price_text}, {loc} — {status}\n"
        f"Перевірка: кожні {m.get('check_interval_minutes', 180)} хв"
    )


def _fmt_saved_line(s: dict) -> str:
    status_labels = {"interest": "⭐ Цікаво", "bought": "🛒 Куплено", "sold": "💰 Перепродано", "rejected": "❌ Відмовився"}
    return (
        f"📦 *{s.get('title') or '—'}*\n"
        f"💰 Купівля: {s.get('purchase_price', '?')} {s.get('currency', '')}\n"
        f"📈 Оцінка: {s.get('score', '?')}/100, маржа ~{s.get('margin', '?')}%\n"
        f"📌 Статус: {status_labels.get(s.get('status'), s.get('status'))}\n"
        f"🔗 {s.get('url', '')}"
    )


def _ikb_saved_item(item_id: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "interest":
        rows.append([InlineKeyboardButton(text="🛒 Купив", callback_data=f"rss_status:{item_id}:bought")])
    if status == "bought":
        rows.append([InlineKeyboardButton(text="💰 Перепродав", callback_data=f"rss_status:{item_id}:sold")])
    if status in ("interest", "bought"):
        rows.append([InlineKeyboardButton(text="❌ Відмовився", callback_data=f"rss_status:{item_id}:rejected")])
    rows.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"rss_del:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================================================
# ВХІД
# =========================================================

@router.message(F.text.in_(["🔎 Знайти перепродаж", "📈 Статистика перепродажу"]))
async def resale_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if msg.text == "📈 Статистика перепродажу":
        return await _show_stats(msg)
    await msg.answer(
        "🔥 *Знайти перепродаж — AI-моніторинг OLX*\n\n"
        "Створи моніторинг за критеріями, і я постійно шукатиму нові вигідні "
        "оголошення та надсилатиму найкращі знахідки автоматично.",
        reply_markup=_ikb_resale_menu(),
    )


@router.callback_query(F.data == "rsm_new")
async def resale_new_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ResaleMonitor.category)
    await state.update_data(edit_id=None)
    await cb.answer()
    await cb.message.answer(
        "🏷 Яку категорію/товар шукаємо? (напр. `телефони`, `PlayStation`, `ноутбуки`):",
        reply_markup=kb_cancel(),
    )


# =========================================================
# МАЙСТЕР СТВОРЕННЯ / РЕДАГУВАННЯ МОНІТОРИНГУ
# =========================================================

@router.message(ResaleMonitor.category)
async def rm_category(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(category=msg.text.strip())
    await state.set_state(ResaleMonitor.keywords)
    await msg.answer("🔑 Додаткові ключові слова (напр. `128GB`), або «немає»:")


@router.message(ResaleMonitor.keywords)
async def rm_keywords(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    keywords = "" if raw.lower() in NONE_WORDS else raw
    await state.update_data(keywords=keywords)
    await state.set_state(ResaleMonitor.location)
    await msg.answer("📍 Місто пошуку, або «немає» — шукати по всій Україні:")


@router.message(ResaleMonitor.location)
async def rm_location(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    location = "" if raw.lower() in NONE_WORDS else raw
    await state.update_data(location=location, radius_km=100 if location else 0)
    await state.set_state(ResaleMonitor.min_price)
    await msg.answer("💰 Мінімальна ціна покупки (грн), або «немає»:")


@router.message(ResaleMonitor.min_price)
async def rm_min_price(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val == "invalid":
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(min_price=val)
    await state.set_state(ResaleMonitor.max_price)
    await msg.answer("💰 Максимальна ціна покупки (грн), або «немає»:")


@router.message(ResaleMonitor.max_price)
async def rm_max_price(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val == "invalid":
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(max_price=val)
    await state.set_state(ResaleMonitor.min_profit)
    await msg.answer("💵 Бажаний мінімальний прибуток (грн), або «немає»:")


@router.message(ResaleMonitor.min_profit)
async def rm_min_profit(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val == "invalid":
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(min_profit=val)
    await state.set_state(ResaleMonitor.min_margin)
    await msg.answer("📈 Мінімальна маржа у %, або «немає»:")


@router.message(ResaleMonitor.min_margin)
async def rm_min_margin(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    val = _parse_number(msg.text)
    if val == "invalid":
        return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(min_margin_percent=val)
    await state.set_state(ResaleMonitor.condition)
    await msg.answer("🏷 Стан товару:", reply_markup=_CONDITION_KB)


@router.message(ResaleMonitor.condition)
async def rm_condition(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    key = msg.text.strip().lower()
    if key not in _CONDITION_MAP:
        return await msg.answer("⚠️ Обери варіант на клавіатурі:", reply_markup=_CONDITION_KB)
    await state.update_data(condition=_CONDITION_MAP[key])
    await state.set_state(ResaleMonitor.brand_model)
    await msg.answer("🔧 Конкретний бренд/модель (за потреби), або «немає»:", reply_markup=kb_cancel())


@router.message(ResaleMonitor.brand_model)
async def rm_brand_model(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    brand_model = "" if raw.lower() in NONE_WORDS else raw
    await state.update_data(brand_model=brand_model)
    await state.set_state(ResaleMonitor.frequency)
    await msg.answer("⏱ Як часто перевіряти?", reply_markup=_FREQ_KB)


@router.message(ResaleMonitor.frequency)
async def rm_frequency(msg: Message, state: FSMContext):
    if _is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    minutes = _FREQ_MAP.get(msg.text.strip())
    if minutes is None:
        return await msg.answer("⚠️ Обери варіант на клавіатурі:", reply_markup=_FREQ_KB)

    fd = await state.get_data()
    await state.clear()
    fd["check_interval_minutes"] = minutes

    edit_id = fd.pop("edit_id", None)
    uid = msg.from_user.id
    if edit_id:
        await resale_db.update_monitor(edit_id, uid, fd)
        monitor_id = edit_id
        verb = "оновлено"
    else:
        monitor_id = await resale_db.add_monitor(uid, fd)
        verb = "створено"

    await msg.answer(f"✅ Моніторинг {verb}! Перевіряю перші результати...", reply_markup=kb_main())

    monitor = await resale_db.get_monitor(monitor_id)
    await _run_first_scan(msg, monitor)


async def _run_first_scan(msg: Message, monitor: dict):
    try:
        opportunities, error = await resale_service.scan_monitor(monitor)
    except Exception:
        logger.exception("resale: перший скан моніторингу упав, monitor=%s", monitor.get("_id"))
        return

    if error == "ai_unavailable":
        return await msg.answer(AI_ERROR_TEXT)
    if error == "ai_limit":
        return await msg.answer("⚠️ Денний AI-ліміт вичерпано, перша перевірка запуститься автоматично пізніше.")
    if error == "search_failed":
        return await msg.answer("⚠️ OLX тимчасово недоступний, спробую пізніше автоматично.")
    if not opportunities:
        return await msg.answer("📭 Поки що нічого підходящого не знайшов. Повідомлю, щойно з'явиться вигідний варіант.")

    from config.settings import RESALE_MIN_SCORE_THRESHOLD, RESALE_MAX_NOTIFY_PER_CYCLE
    sent = 0
    urls = []
    for opp in opportunities:
        url = opp["listing"].get("url")
        if url:
            urls.append(url)
        if opp["score"] < RESALE_MIN_SCORE_THRESHOLD or sent >= RESALE_MAX_NOTIFY_PER_CYCLE:
            continue
        pending_id = resale_service.register_pending(monitor["_id"], opp)
        await msg.answer(
            resale_service.format_notification(opp),
            reply_markup=resale_service.ikb_notification(pending_id, url),
        )
        sent += 1

    if urls:
        await resale_db.mark_seen(monitor["_id"], urls)
    await resale_db.increment_stat(monitor["_id"], "found", len(opportunities))


# =========================================================
# КЕРУВАННЯ МОНІТОРИНГАМИ (п.14 ТЗ)
# =========================================================

@router.callback_query(F.data == "rsm_list")
async def resale_list_cb(cb: CallbackQuery):
    await cb.answer()
    monitors = await resale_db.get_user_monitors(cb.from_user.id)
    if not monitors:
        return await cb.message.answer("📭 У тебе ще немає моніторингів. Тисни «➕ Новий моніторинг».")
    for m in monitors:
        await cb.message.answer(_fmt_monitor_line(m), reply_markup=_ikb_monitor(m))


@router.callback_query(F.data.startswith("rsm_pause:"))
async def resale_pause_cb(cb: CallbackQuery):
    mid = cb.data.split(":", 1)[1]
    ok = await resale_db.set_monitor_status(mid, cb.from_user.id, "paused")
    await cb.answer("Призупинено ⏸️" if ok else "Не знайдено", show_alert=not ok)


@router.callback_query(F.data.startswith("rsm_resume:"))
async def resale_resume_cb(cb: CallbackQuery):
    mid = cb.data.split(":", 1)[1]
    ok = await resale_db.set_monitor_status(mid, cb.from_user.id, "active")
    await cb.answer("Запущено ▶️" if ok else "Не знайдено", show_alert=not ok)


@router.callback_query(F.data.startswith("rsm_del:"))
async def resale_delete_cb(cb: CallbackQuery):
    mid = cb.data.split(":", 1)[1]
    ok = await resale_db.delete_monitor(mid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Моніторинг видалено.")
        except Exception:
            pass


@router.callback_query(F.data.startswith("rsm_mstats:"))
async def resale_monitor_stats_cb(cb: CallbackQuery):
    mid = cb.data.split(":", 1)[1]
    monitor = await resale_db.get_monitor(mid)
    await cb.answer()
    if not monitor or monitor.get("uid") != cb.from_user.id:
        return await cb.message.answer("⚠️ Моніторинг не знайдено.")
    stats = monitor.get("stats") or {}
    await cb.message.answer(
        f"📊 *Статистика моніторингу «{monitor.get('category')}»*\n\n"
        f"🔎 Знайдено можливостей: {stats.get('found', 0)}\n"
        f"⭐ Збережено: {stats.get('saved', 0)}\n"
        f"🛒 Куплено: {stats.get('bought', 0)}\n"
        f"💰 Перепродано: {stats.get('sold', 0)}"
    )


@router.callback_query(F.data.startswith("rsm_edit:"))
async def resale_edit_cb(cb: CallbackQuery, state: FSMContext):
    mid = cb.data.split(":", 1)[1]
    monitor = await resale_db.get_monitor(mid)
    await cb.answer()
    if not monitor or monitor.get("uid") != cb.from_user.id:
        return await cb.message.answer("⚠️ Моніторинг не знайдено.")
    await state.update_data(edit_id=mid)
    await state.set_state(ResaleMonitor.category)
    await cb.message.answer(
        f"✏️ Редагування моніторингу. Поточна категорія: *{monitor.get('category')}*\n\n"
        "Введи нову категорію/товар (або той самий текст, щоб лишити без змін):",
        reply_markup=kb_cancel(),
    )


# =========================================================
# ЗБЕРЕЖЕНІ МОЖЛИВОСТІ (п.10 ТЗ)
# =========================================================

async def _show_saved(msg_or_cb_message, uid: int):
    saved = await resale_db.get_saved(uid)
    if not saved:
        return await msg_or_cb_message.answer("📭 Немає збережених можливостей.")
    for s in saved:
        await msg_or_cb_message.answer(_fmt_saved_line(s), reply_markup=_ikb_saved_item(str(s["_id"]), s.get("status", "interest")))


@router.callback_query(F.data == "rsm_saved")
async def resale_saved_cb(cb: CallbackQuery):
    await cb.answer()
    await _show_saved(cb.message, cb.from_user.id)


@router.message(F.text == "⭐ Збережені можливості")
async def resale_saved_msg(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await _show_saved(msg, msg.from_user.id)
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.callback_query(F.data.startswith("rss_status:"))
async def resale_saved_status_cb(cb: CallbackQuery):
    _, item_id, status = cb.data.split(":")
    ok = await resale_db.set_saved_status(item_id, cb.from_user.id, status)
    await cb.answer("Оновлено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        item = await resale_db.get_saved_item(item_id)
        if item and item.get("monitor_id"):
            if status == "bought":
                await resale_db.increment_stat(item["monitor_id"], "bought")
            elif status == "sold":
                await resale_db.increment_stat(item["monitor_id"], "sold")
                await resale_db.increment_stat(item["monitor_id"], "actual_profit", item.get("profit") or 0)
        try:
            await cb.message.edit_reply_markup(reply_markup=_ikb_saved_item(item_id, status))
        except Exception:
            pass


@router.callback_query(F.data.startswith("rss_del:"))
async def resale_saved_delete_cb(cb: CallbackQuery):
    item_id = cb.data.split(":", 1)[1]
    ok = await resale_db.delete_saved(item_id, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Видалено зі збережених.")
        except Exception:
            pass


# =========================================================
# СТАТИСТИКА (п.11 ТЗ)
# =========================================================

async def _show_stats(target: Message):
    uid = target.from_user.id
    monitors = await resale_db.get_user_monitors(uid)
    saved = await resale_db.get_saved(uid)
    await target.answer(resale_service.build_statistics_text(monitors, saved), reply_markup=kb_main())


@router.callback_query(F.data == "rsm_stats")
async def resale_stats_cb(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    monitors = await resale_db.get_user_monitors(uid)
    saved = await resale_db.get_saved(uid)
    await cb.message.answer(resale_service.build_statistics_text(monitors, saved))


# =========================================================
# КНОПКИ ПІД СПОВІЩЕННЯМ (п.9, п.12 ТЗ)
# =========================================================

@router.callback_query(F.data.startswith("rso_analyze:"))
async def resale_opp_analyze_cb(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    opp = resale_service.get_pending(pid)
    await cb.answer()
    if not opp:
        return await cb.message.answer("⚠️ Ця знахідка вже застаріла, дочекайся нового сповіщення.")
    text = resale_engine.format_analysis(opp["listing"], opp["analysis"], cached=True)
    await cb.message.answer(text)


@router.callback_query(F.data.startswith("rso_save:"))
async def resale_opp_save_cb(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    opp = resale_service.get_pending(pid)
    await cb.answer("Збережено ⭐" if opp else "Застаріло", show_alert=not opp)
    if not opp:
        return
    await resale_db.save_opportunity(cb.from_user.id, opp.get("monitor_id"), opp)
    if opp.get("monitor_id"):
        await resale_db.increment_stat(opp["monitor_id"], "saved")
        await resale_db.increment_stat(opp["monitor_id"], "potential_profit", opp.get("profit") or 0)
        keyword = opp["analysis"].get("item_brand") or opp["analysis"].get("item_name")
        await resale_db.add_learned_keyword(opp["monitor_id"], keyword, positive=True)


@router.callback_query(F.data.startswith("rso_skip:"))
async def resale_opp_skip_cb(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    opp = resale_service.get_pending(pid)
    await cb.answer("Ок, врахую 👍")
    if not opp or not opp.get("monitor_id"):
        return
    keyword = opp["analysis"].get("item_brand") or opp["analysis"].get("item_name")
    await resale_db.add_learned_keyword(opp["monitor_id"], keyword, positive=False)


@router.callback_query(F.data.startswith("rso_block:"))
async def resale_opp_block_cb(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    opp = resale_service.get_pending(pid)
    await cb.answer("Більше не шукатиму подібне 🔕")
    if not opp or not opp.get("monitor_id"):
        return
    keyword = opp["analysis"].get("item_brand") or opp["analysis"].get("item_name")
    await resale_db.add_blocked_similar(opp["monitor_id"], keyword)