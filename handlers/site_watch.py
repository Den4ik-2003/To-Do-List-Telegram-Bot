"""
ЗМІНЕНИЙ ФАЙЛ: handlers/site_watch.py

НОВЕ (фіча "сайт → його сторінки"): до кожного сайту (кнопка "🧪 QA скан" /
uptime-запису) тепер можна прив'язати довільну кількість сторінок двома
способами:

1. "🔎 Знайти сторінки автоматично" (sw_discover_cb) — бот завантажує
   головну сторінку сайту, шукає на ній внутрішні посилання
   (services/site_watch_service.py:discover_page_urls) і показує їх
   чекбокс-списком (за замовчуванням усі позначені — досить одразу
   натиснути "Додати", якщо потрібні всі). Стан вибору живе в
   discover_drafts_cache (in-memory, per draft_id — той самий патерн, що
   voice_task_drafts у handlers/common.py).

2. "✏️ Додати вручну" (sw_add_manual_cb) — користувач вставляє один чи
   кілька URL одним повідомленням, кожен з нового рядка.

Обидва шляхи ведуть у add_page_watch(..., site_id=wid) — тому додані
сторінки лишаються видимими в тому самому "📋 Список моніторингів", але
тепер позначені, частиною якого сайту вони є (build_page_card_text
показує "🔗 Частина сайту: ...").

Решта файлу — без змін.
"""

import logging
import secrets

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import site_watch as site_watch_db
from services import site_watch_service
from config.settings import PAGE_HISTORY_DISPLAY_LIMIT
from keyboards.main_menu import kb_main, kb_cancel, kb_category, CATEGORY_LIFE
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="site_watch")

# НОВЕ: чернетки автопошуку сторінок сайту до підтвердження — та сама
# логіка, що voice_task_drafts у handlers/common.py. Ключ — випадковий
# draft_id (не uid!), щоб callback_data лишався коротким.
discover_drafts_cache: dict = {}

MAX_MANUAL_URLS_PER_MESSAGE = 20


class SiteWatch(StatesGroup):
    waiting_url = State()
    waiting_page_url = State()
    waiting_label = State()
    waiting_multi_pages_url = State()  # НОВЕ: ручне додавання кількох сторінок сайту


def _ikb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати сайт (uptime)", callback_data="sw_add")],
        [InlineKeyboardButton(text="📄 Додати сторінку (AI-аналіз змін)", callback_data="sw_add_page")],
        [InlineKeyboardButton(text="📋 Список моніторингів", callback_data="sw_list")],
        [InlineKeyboardButton(text="📈 Загальна статистика", callback_data="sw_stats_global")],
    ])


@router.message(F.text == "🌐 Моніторинг сайтів")
async def site_watch_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "🌐 *Моніторинг сайтів та сторінок*\n\n"
        "• *Сайт* — перевіряю доступність (uptime), можу запустити QA-скан, "
        "і до нього можна прив'язати всі сторінки, які хочеш стежити.\n"
        "• *Сторінка* — стежу за змістом (ціни, наявність товару, вакансії тощо) "
        "і AI пояснює, що саме змінилося та чи це важливо.",
        reply_markup=_ikb_menu(),
    )


# ---------------- Uptime-моніторинг (без змін у поведінці) ----------------

@router.callback_query(F.data == "sw_add")
async def site_watch_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SiteWatch.waiting_url)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав адресу сайту (напр. `example.com` або `https://example.com`):",
        reply_markup=kb_cancel(),
    )


@router.message(SiteWatch.waiting_url, F.text == "❌ Скасувати")
async def site_watch_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_category(CATEGORY_LIFE))


@router.message(SiteWatch.waiting_url)
async def site_watch_add_url(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer(
            "⚠️ Надішли, будь ласка, текстову адресу сайту (або натисни ❌ Скасувати):",
            reply_markup=kb_cancel(),
        )

    raw = msg.text.strip()
    url = site_watch_service.normalize_url(raw)

    if "." not in url:
        return await msg.answer("⚠️ Схоже, це не адреса сайту. Спробуй ще раз:")

    await state.clear()

    wait_msg = await msg.answer("🔎 Перевіряю сайт...")
    is_up = await site_watch_service.check_site(url)

    await site_watch_db.add_site_watch(msg.from_user.id, url)

    status_line = "🟢 зараз доступний" if is_up else "🔴 зараз НЕ відповідає"
    await wait_msg.edit_text(
        f"✅ Додано до моніторингу!\n\n🌐 `{url}`\n{status_line}\n\n"
        f"Перевірятиму регулярно і одразу повідомлю, якщо статус зміниться.\n\n"
        f"💡 Тепер можеш додати до нього сторінки для AI-аналізу змін — "
        f"у списку моніторингів з'явиться кнопка «📄 Сторінки сайту»."
    )
    await msg.answer("Що далі?", reply_markup=_ikb_menu())


@router.callback_query(F.data.startswith("sw_qa:"))
async def site_watch_qa_run(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch:
        return await cb.answer("Не знайдено", show_alert=True)

    await cb.answer("Запускаю QA-скан, це займе трохи часу...")
    wait_msg = await cb.message.answer("🧪 Сканую сайт (сторінки, форми, посилання, швидкість)...")

    try:
        report = await site_watch_service.run_qa_scan(watch["url"])
    except Exception:
        logger.exception("QA scan failed for watch=%s", wid)
        return await wait_msg.edit_text("⚠️ Не вдалося провести скан. Спробуй пізніше.")

    is_ok = not report.get("critical_error") and not report.get("broken_pages") and \
        not report.get("form_issues") and not report.get("broken_images")

    await site_watch_db.save_qa_result(wid, watch["uid"], watch["url"], report, is_ok)

    text = site_watch_service.format_qa_report(report)
    await wait_msg.edit_text(text)


@router.callback_query(F.data.startswith("sw_del:"))
async def site_watch_delete(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    ok = await site_watch_db.delete_watch(wid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Видалено з моніторингу.")
        except Exception:
            pass


# ---------------- Список (uptime + page разом) ----------------

@router.callback_query(F.data == "sw_list")
async def site_watch_list(cb: CallbackQuery):
    watches = await site_watch_db.get_user_watches(cb.from_user.id)
    await cb.answer()

    if not watches:
        return await cb.message.answer("📭 У тебе ще немає активних моніторингів.")

    # НОВЕ: щоб показати "🔗 Частина сайту: ..." на картці сторінки, наперед
    # збираємо label-и всіх сайтів користувача за їхніми _id.
    site_labels = {
        str(w["_id"]): (w.get("label") or w.get("url", ""))
        for w in watches if w.get("kind", "uptime") == "uptime"
    }

    for w in watches:
        kind = w.get("kind", "uptime")

        if kind == "page":
            site_label = site_labels.get(w.get("site_id")) if w.get("site_id") else None
            text = site_watch_service.build_page_card_text(w, site_label=site_label)
            wid = str(w["_id"])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Історія", callback_data=f"sw_hist:{wid}"),
                 InlineKeyboardButton(text="🤖 AI-звіт", callback_data=f"sw_report:{wid}")],
                [InlineKeyboardButton(text="⏱ Частота", callback_data=f"sw_freq:{wid}"),
                 InlineKeyboardButton(text="🔔 Сповіщення", callback_data=f"sw_notif:{wid}")],
                [InlineKeyboardButton(text="✏️ Назва", callback_data=f"sw_label:{wid}"),
                 InlineKeyboardButton(text="🗑 Видалити", callback_data=f"sw_del:{wid}")],
            ])
            await cb.message.answer(text, reply_markup=kb)
        else:
            wid = str(w["_id"])
            status = w.get("last_status")
            icon = "🟢" if status is True else ("🔴" if status is False else "⏳")
            qa_ok = w.get("last_qa_ok")
            qa_line = ""
            if qa_ok is True:
                qa_line = "\n🧪 Останній QA: ✅ ок"
            elif qa_ok is False:
                qa_line = "\n🧪 Останній QA: ⚠️ є проблеми"

            # НОВЕ: скільки сторінок прив'язано до цього сайту
            pages = await site_watch_db.get_pages_for_site(wid)
            pages_line = f"\n📄 Сторінок під наглядом: {len(pages)}" if pages else ""

            text = f"{icon} {w.get('url', '')}{qa_line}{pages_line}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧪 QA скан", callback_data=f"sw_qa:{wid}"),
                 InlineKeyboardButton(text=f"📄 Сторінки сайту ({len(pages)})", callback_data=f"sw_pages:{wid}")],
                [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"sw_del:{wid}")],
            ])
            await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "sw_stats_global")
async def site_watch_global_stats(cb: CallbackQuery):
    watches = await site_watch_db.get_user_watches(cb.from_user.id)
    await cb.answer()
    if not watches:
        return await cb.message.answer("📭 У тебе ще немає моніторингів.")
    await cb.message.answer(site_watch_service.format_global_stats(watches))


# ---------------- Додавання окремої сторінки (без прив'язки до сайту) ----------------

@router.callback_query(F.data == "sw_add_page")
async def site_watch_add_page_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SiteWatch.waiting_page_url)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав адресу сторінки для моніторингу змін "
        "(напр. сторінка товару, вакансій, цін, акцій):",
        reply_markup=kb_cancel(),
    )


@router.message(SiteWatch.waiting_page_url, F.text == "❌ Скасувати")
async def site_watch_add_page_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_category(CATEGORY_LIFE))


@router.message(SiteWatch.waiting_page_url)
async def site_watch_add_page_url(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстову адресу сторінки (або ❌ Скасувати):", reply_markup=kb_cancel())

    raw = msg.text.strip()
    url = site_watch_service.normalize_url(raw)
    if "." not in url:
        return await msg.answer("⚠️ Схоже, це не адреса сторінки. Спробуй ще раз:")

    await state.clear()

    wait_msg = await msg.answer("🔎 Завантажую сторінку для першого знімку...")
    result = await site_watch_service.fetch_page_content_text(url)

    if result is None or result.get("error"):
        reason = (result or {}).get("error", "невідома помилка")
        return await wait_msg.edit_text(
            f"⚠️ Не вдалося завантажити сторінку для аналізу.\n\n🌐 `{url}`\nПричина: {reason}\n\n"
            f"Перевір адресу або спробуй додати пізніше."
        )

    wid = await site_watch_db.add_page_watch(msg.from_user.id, url)
    await site_watch_db.update_content_snapshot(wid, site_watch_service.hash_content(result["text"]), result["text"])

    await wait_msg.edit_text(
        f"✅ Сторінку додано до моніторингу!\n\n🌐 `{url}`\n\n"
        f"Зробив перший знімок вмісту. З наступної перевірки почну порівнювати "
        f"зміни й пояснювати їх через AI."
    )
    await msg.answer("Що далі?", reply_markup=_ikb_menu())


# ---------------- НОВЕ: сторінки конкретного сайту ----------------

@router.callback_query(F.data.startswith("sw_pages:"))
async def site_watch_pages_menu(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    pages = await site_watch_db.get_pages_for_site(wid)
    await cb.answer()

    label = watch.get("label") or watch.get("url", "")
    text = f"📄 *Сторінки сайту* — {label}\n\n"
    if pages:
        text += f"Уже під наглядом: {len(pages)} шт. (переглянути можна в «📋 Список моніторингів»).\n\n"
    else:
        text += "Поки що жодної сторінки не додано.\n\n"
    text += "Як хочеш додати сторінки?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Знайти автоматично", callback_data=f"sw_discover:{wid}")],
        [InlineKeyboardButton(text="✏️ Додати вручну (список URL)", callback_data=f"sw_add_manual:{wid}")],
    ])
    await cb.message.answer(text, reply_markup=kb)


def _ikb_discover(draft_id: str, urls: list[str], selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for i, url in enumerate(urls):
        mark = "☑" if i in selected else "☐"
        short = url if len(url) <= 42 else url[:39] + "…"
        rows.append([InlineKeyboardButton(text=f"{mark} {short}", callback_data=f"sw_disc_toggle:{draft_id}:{i}")])
    rows.append([
        InlineKeyboardButton(text=f"✅ Додати обрані ({len(selected)})", callback_data=f"sw_disc_confirm:{draft_id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"sw_disc_cancel:{draft_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("sw_discover:"))
async def site_watch_discover_cb(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    await cb.answer("Шукаю сторінки на сайті...")
    wait_msg = await cb.message.answer("🔎 Завантажую головну сторінку і шукаю посилання...")

    urls = await site_watch_service.discover_page_urls(watch["url"])
    if urls is None:
        return await wait_msg.edit_text("⚠️ Не вдалося завантажити головну сторінку сайту. Спробуй пізніше.")

    already = {p["url"] for p in await site_watch_db.get_pages_for_site(wid)}
    urls = [u for u in urls if u not in already]

    if not urls:
        return await wait_msg.edit_text(
            "📭 Нових сторінок не знайшов (усі знайдені посилання вже додані, "
            "або на головній немає внутрішніх посилань)."
        )

    draft_id = secrets.token_hex(4)
    discover_drafts_cache[draft_id] = {
        "uid": cb.from_user.id,
        "site_id": wid,
        "urls": urls,
        "selected": set(range(len(urls))),  # за замовчуванням — усі позначені
    }

    await wait_msg.edit_text(
        f"🔎 Знайшов {len(urls)} нових сторінок. Усі позначені за замовчуванням — "
        f"можеш зняти зайві або одразу тиснути «✅ Додати обрані»:",
        reply_markup=_ikb_discover(draft_id, urls, discover_drafts_cache[draft_id]["selected"]),
    )


@router.callback_query(F.data.startswith("sw_disc_toggle:"))
async def site_watch_discover_toggle_cb(cb: CallbackQuery):
    _, draft_id, idx_s = cb.data.split(":")
    draft = discover_drafts_cache.get(draft_id)
    if not draft or draft["uid"] != cb.from_user.id:
        return await cb.answer("Список застарів, почни пошук ще раз.", show_alert=True)

    idx = int(idx_s)
    if idx in draft["selected"]:
        draft["selected"].discard(idx)
    else:
        draft["selected"].add(idx)

    try:
        await cb.message.edit_reply_markup(reply_markup=_ikb_discover(draft_id, draft["urls"], draft["selected"]))
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("sw_disc_cancel:"))
async def site_watch_discover_cancel_cb(cb: CallbackQuery):
    draft_id = cb.data.split(":", 1)[1]
    draft = discover_drafts_cache.pop(draft_id, None)
    if draft and draft["uid"] != cb.from_user.id:
        discover_drafts_cache[draft_id] = draft  # чужий чорновик — не чіпаємо
        return await cb.answer("Не твій список.", show_alert=True)
    try:
        await cb.message.edit_text("Скасовано.")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("sw_disc_confirm:"))
async def site_watch_discover_confirm_cb(cb: CallbackQuery):
    draft_id = cb.data.split(":", 1)[1]
    draft = discover_drafts_cache.get(draft_id)
    if not draft or draft["uid"] != cb.from_user.id:
        return await cb.answer("Список застарів, почни пошук ще раз.", show_alert=True)

    selected_urls = [draft["urls"][i] for i in sorted(draft["selected"])]
    discover_drafts_cache.pop(draft_id, None)

    if not selected_urls:
        return await cb.answer("Нічого не обрано.", show_alert=True)

    await cb.answer(f"Додаю {len(selected_urls)} сторінок...")
    try:
        await cb.message.edit_text(f"⏳ Додаю {len(selected_urls)} сторінок і роблю перші знімки...")
    except Exception:
        pass

    added, failed = 0, 0
    for url in selected_urls:
        result = await site_watch_service.fetch_page_content_text(url)
        if not result or result.get("error"):
            failed += 1
            continue
        wid = await site_watch_db.add_page_watch(cb.from_user.id, url, site_id=draft["site_id"])
        await site_watch_db.update_content_snapshot(wid, site_watch_service.hash_content(result["text"]), result["text"])
        added += 1

    summary = f"✅ Додано {added} сторінок."
    if failed:
        summary += f"\n⚠️ Не вдалося завантажити {failed} — спробуй додати їх вручну пізніше."
    try:
        await cb.message.edit_text(summary)
    except Exception:
        await cb.message.answer(summary)


@router.callback_query(F.data.startswith("sw_add_manual:"))
async def site_watch_add_manual_cb(cb: CallbackQuery, state: FSMContext):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    await state.set_state(SiteWatch.waiting_multi_pages_url)
    await state.update_data(site_id=wid)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав адреси сторінок — кожну з нового рядка "
        f"(максимум {MAX_MANUAL_URLS_PER_MESSAGE} за раз):",
        reply_markup=kb_cancel(),
    )


@router.message(SiteWatch.waiting_multi_pages_url, F.text == "❌ Скасувати")
async def site_watch_add_manual_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_category(CATEGORY_LIFE))


@router.message(SiteWatch.waiting_multi_pages_url)
async def site_watch_add_manual_urls(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстом одну або кілька адрес (або ❌ Скасувати):", reply_markup=kb_cancel())

    data = await state.get_data()
    site_id = data.get("site_id")
    await state.clear()
    if not site_id:
        return await msg.answer("⚠️ Щось пішло не так, спробуй ще раз через список моніторингів.", reply_markup=kb_category(CATEGORY_LIFE))

    raw_lines = [line.strip() for line in msg.text.splitlines() if line.strip()]
    raw_lines = raw_lines[:MAX_MANUAL_URLS_PER_MESSAGE]
    if not raw_lines:
        return await msg.answer("⚠️ Не знайшов жодної адреси. Спробуй ще раз:", reply_markup=kb_cancel())

    already = {p["url"] for p in await site_watch_db.get_pages_for_site(site_id)}
    wait_msg = await msg.answer(f"⏳ Додаю {len(raw_lines)} сторінок...")

    added, skipped, failed = 0, 0, 0
    for raw in raw_lines:
        url = site_watch_service.normalize_url(raw)
        if "." not in url:
            failed += 1
            continue
        if url in already:
            skipped += 1
            continue

        result = await site_watch_service.fetch_page_content_text(url)
        if not result or result.get("error"):
            failed += 1
            continue

        wid = await site_watch_db.add_page_watch(msg.from_user.id, url, site_id=site_id)
        await site_watch_db.update_content_snapshot(wid, site_watch_service.hash_content(result["text"]), result["text"])
        already.add(url)
        added += 1

    summary = f"✅ Додано {added} сторінок."
    if skipped:
        summary += f"\n➖ Пропущено {skipped} — вже були додані."
    if failed:
        summary += f"\n⚠️ Не вдалося завантажити {failed}."
    await wait_msg.edit_text(summary)
    await msg.answer("Що далі?", reply_markup=_ikb_menu())


# ---------------- Історія та AI-звіт ----------------

@router.callback_query(F.data.startswith("sw_hist:"))
async def site_watch_history(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)
    await cb.answer()
    history = await site_watch_db.get_page_history(wid, limit=PAGE_HISTORY_DISPLAY_LIMIT)
    await cb.message.answer(site_watch_service.format_history(watch.get("label", watch["url"]), history))


@router.callback_query(F.data.startswith("sw_report:"))
async def site_watch_ai_report(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    history = await site_watch_db.get_page_history(wid, limit=30)
    if len(history) < 2:
        return await cb.answer(
            "Ще недостатньо даних для AI-звіту — потрібно накопичити більше реальних змін.",
            show_alert=True,
        )

    await cb.answer()
    wait_msg = await cb.message.answer("🤖 Формую AI-звіт на основі історії змін...")
    report = await site_watch_service.build_ai_trend_report(watch["url"], history)
    if not report:
        return await wait_msg.edit_text("⚠️ Не вдалося сформувати AI-звіт. Спробуй пізніше.")
    await wait_msg.edit_text(site_watch_service.format_ai_report(watch.get("label", watch["url"]), report))


# ---------------- Частота перевірки ----------------

@router.callback_query(F.data.startswith("sw_freq:"))
async def site_watch_freq_menu(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)
    await cb.answer()

    current = watch.get("check_interval_minutes", 60)
    buttons, row = [], []
    for minutes in site_watch_service.FREQ_OPTIONS:
        mark = "✅ " if minutes == current else ""
        row.append(InlineKeyboardButton(
            text=f"{mark}{site_watch_service.format_interval(minutes)}",
            callback_data=f"sw_freq_set:{wid}:{minutes}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await cb.message.answer("⏱ Обери частоту перевірки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("sw_freq_set:"))
async def site_watch_freq_set(cb: CallbackQuery):
    _, wid, minutes = cb.data.split(":")
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    await site_watch_db.update_watch_settings(wid, cb.from_user.id, check_interval_minutes=int(minutes))
    await cb.answer(f"Частоту оновлено: {site_watch_service.format_interval(int(minutes))} ✅")
    try:
        await cb.message.delete()
    except Exception:
        pass


# ---------------- Сповіщення ----------------

@router.callback_query(F.data.startswith("sw_notif:"))
async def site_watch_notif_menu(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)
    await cb.answer()

    on = watch.get("notifications_enabled", True)
    moderate = watch.get("notify_moderate", False)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔕 Вимкнути всі" if on else "🔔 Увімкнути всі",
            callback_data=f"sw_notif_toggle:{wid}",
        )],
        [InlineKeyboardButton(
            text=f"🟡 Помірні зміни: {'✅ увімк.' if moderate else '❌ вимк.'}",
            callback_data=f"sw_notif_moderate:{wid}",
        )],
    ])
    await cb.message.answer(
        "🔔 Налаштування сповіщень\n\n"
        "🔴 Важливі зміни повідомляються завжди (якщо сповіщення взагалі не вимкнено).",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sw_notif_toggle:"))
async def site_watch_notif_toggle(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    new_state = not watch.get("notifications_enabled", True)
    await site_watch_db.update_watch_settings(wid, cb.from_user.id, notifications_enabled=new_state)
    await cb.answer("Сповіщення увімкнено ✅" if new_state else "Сповіщення вимкнено 🔕")
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("sw_notif_moderate:"))
async def site_watch_notif_moderate_toggle(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    new_state = not watch.get("notify_moderate", False)
    await site_watch_db.update_watch_settings(wid, cb.from_user.id, notify_moderate=new_state)
    await cb.answer("Помірні зміни: увімкнено ✅" if new_state else "Помірні зміни: вимкнено ❌")
    try:
        await cb.message.delete()
    except Exception:
        pass


# ---------------- Редагування назви ----------------

@router.callback_query(F.data.startswith("sw_label:"))
async def site_watch_label_start(cb: CallbackQuery, state: FSMContext):
    wid = cb.data.split(":", 1)[1]
    watch = await site_watch_db.get_watch(wid)
    if not watch or watch.get("uid") != cb.from_user.id:
        return await cb.answer("Не знайдено", show_alert=True)

    await state.set_state(SiteWatch.waiting_label)
    await state.update_data(watch_id=wid)
    await cb.answer()
    await cb.message.answer("✏️ Введи нову назву для цього моніторингу:", reply_markup=kb_cancel())


@router.message(SiteWatch.waiting_label, F.text == "❌ Скасувати")
async def site_watch_label_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_category(CATEGORY_LIFE))


@router.message(SiteWatch.waiting_label)
async def site_watch_label_set(msg: Message, state: FSMContext):
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстову назву (або ❌ Скасувати):", reply_markup=kb_cancel())

    data = await state.get_data()
    wid = data.get("watch_id")
    await state.clear()
    if not wid:
        return await msg.answer("⚠️ Щось пішло не так, спробуй ще раз через список моніторингів.")

    label = msg.text.strip()[:80]
    await site_watch_db.update_watch_settings(wid, msg.from_user.id, label=label)
    await msg.answer(f"✅ Назву оновлено: {label}", reply_markup=kb_category(CATEGORY_LIFE))