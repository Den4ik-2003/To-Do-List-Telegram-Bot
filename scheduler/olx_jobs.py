import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import OLX_CHECK_INTERVAL_MINUTES
from database import olx as olx_db
from services import olx_service
from services import olx_scanner
from services import resale_engine

logger = logging.getLogger("tasks_bot")

# AI Scanner споживає денний AI-ліміт користувача при кожній перевірці,
# тому свідомо перевіряється рідше, ніж звичайні price/search-трекери.
# Реалізовано без окремої apscheduler-джоби: усередині одного циклу
# check_all_olx_trackers просто пропускаємо scanner, якщо з моменту
# last_checked_at пройшло менше SCANNER_MIN_INTERVAL_MINUTES.
SCANNER_MIN_INTERVAL_MINUTES = max(OLX_CHECK_INTERVAL_MINUTES * 3, 60)


async def _check_listing(bot: Bot, tracker: dict):
    url = tracker.get("url")
    old_price = tracker.get("last_price")
    price_data = await olx_service.fetch_listing_price(url)
    if not price_data:
        return

    new_price, currency = price_data
    if old_price is None:
        await olx_db.update_listing_price(tracker["_id"], new_price, currency)
        return

    if new_price < old_price:
        diff = new_price - old_price
        # НОВЕ: якщо вже накопичилась історія — одразу показуємо загальний
        # % падіння з моменту додавання в моніторинг, а не лише останній крок.
        summary = olx_db.price_drop_summary(tracker)
        drop_line = ""
        if summary:
            drop_line = f"\n📉 Загалом з початку стеження: −{summary['drop_percent']}%"
        await bot.send_message(
            tracker["uid"],
            f"🔥 *Ціна впала!*\n\n"
            f"🔗 {url}\n\n"
            f"Було: *{old_price:.0f} {currency}*\n"
            f"Стало: *{new_price:.0f} {currency}*\n"
            f"↓ {diff:.0f} {currency}"
            f"{drop_line}",
        )

    if new_price != old_price:
        await olx_db.update_listing_price(tracker["_id"], new_price, currency)


async def _check_search(bot: Bot, tracker: dict):
    domain = "olx.pl" if "olx.pl" in (tracker.get("url") or "") else "olx.ua"
    results = await olx_service.search_listings(
        tracker.get("title_query", ""),
        tracker.get("max_price"),
        tracker.get("location", ""),
        tracker.get("radius_km", 0),
        domain=domain,
    )
    if results is None:
        # Технічний збій запиту — НЕ пишемо знімок статистики (п.4 Тренди):
        # якщо це зробити, "0 результатів" від збою виглядатиме як реальне
        # падіння пропозицій і спотворить тренд неправдивими даними.
        return

    seen_ids = set(tracker.get("seen_ids", []))
    new_items = [r for r in results if r["id"] not in seen_ids]

    # НОВЕ: знімок для 🔥 OLX Тренди — тільки реальні дані з цього запиту.
    priced = [r["price"] for r in results if r.get("price") is not None]
    avg_price = round(sum(priced) / len(priced), 2) if priced else None
    currency = results[0]["currency"] if results else "UAH"
    await olx_db.record_search_stat(
        title_query=tracker.get("title_query", ""),
        domain=domain,
        count_total=len(results),
        count_new=len(new_items) if seen_ids else 0,
        avg_price=avg_price,
        currency=currency,
    )

    if not seen_ids:
        await olx_db.update_search_seen_ids(tracker["_id"], [r["id"] for r in results])
        return

    if not new_items:
        return

    for item in new_items[:5]:
        price_text = f"{item['price']:.0f} {item['currency']}" if item.get("price") else "ціна не вказана"
        await bot.send_message(
            tracker["uid"],
            f"🆕 *Знайдено новий варіант*\n\n"
            f"{item['title']}\n"
            f"💵 {price_text}\n"
            f"📍 {item.get('location_text', '')}\n\n"
            f"[Відкрити]({item['url']})",
        )

    all_ids = seen_ids | {r["id"] for r in results}
    await olx_db.update_search_seen_ids(tracker["_id"], list(all_ids))


async def _check_scanner(bot: Bot, tracker: dict):
    """🧠 AI Scanner: фонова перевірка. Мовчки пропускає цикл при AI-ліміті/
    збої пошуку — не варто спамити технічними помилками щогодини, користувач
    і так побачить причину, якщо спробує ручний «🧲 Злови помилку»."""
    last_checked = tracker.get("last_checked_at")
    if last_checked:
        try:
            minutes_since = (datetime.now() - datetime.fromisoformat(last_checked)).total_seconds() / 60
            if minutes_since < SCANNER_MIN_INTERVAL_MINUTES:
                return
        except ValueError:
            pass

    uid = tracker["uid"]
    domain = "olx.pl" if tracker.get("domain") == "olx.pl" else "olx.ua"
    ranked, error = await olx_scanner.scan_for_deals(
        uid, tracker.get("title_query", ""), tracker.get("max_price"),
        tracker.get("location", ""), tracker.get("radius_km", 0), domain=domain,
    )
    await olx_db.update_scanner_state(tracker["_id"], tracker.get("seen_ids", []))

    if error or not ranked:
        return

    seen_ids = set(tracker.get("seen_ids", []))
    fresh = [item for item in ranked if (item.get("_listing") or {}).get("url") not in seen_ids]
    if not fresh:
        return

    # Не більше 2 знахідок за цикл — щоб не перетворити AI Scanner на спам.
    for item in fresh[:2]:
        listing = item.get("_listing") or {}
        analysis = item.get("resale_analysis")
        if not listing or not analysis:
            continue
        text = resale_engine.format_analysis(listing, analysis, cached=False)
        await bot.send_message(
            uid,
            f"🚨 *AI Scanner знайшов можливість*\n\n{text}",
        )

    new_seen = seen_ids | {(item.get("_listing") or {}).get("url") for item in ranked if item.get("_listing")}
    await olx_db.update_scanner_state(tracker["_id"], list(filter(None, new_seen)))


async def check_all_olx_trackers(bot: Bot):
    trackers = await olx_db.get_all_trackers()
    for t in trackers:
        try:
            if t.get("type") == "listing":
                await _check_listing(bot, t)
            elif t.get("type") == "search":
                await _check_search(bot, t)
            elif t.get("type") == "scanner":
                await _check_scanner(bot, t)
        except Exception:
            logger.exception("OLX tracker check failed for tracker=%s", t.get("_id"))


def register_olx_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(
        check_all_olx_trackers,
        "interval",
        minutes=OLX_CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="olx_check",
        replace_existing=True,
    )