import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import shop_articles as articles_db
from keyboards.main_menu import kb_main
from keyboards.shops import (
    ikb_articles_menu,
    kb_cancel_article,
    ikb_article_add_duplicate,
    kb_article_more,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="shop_articles")

CANCEL_TEXT = "❌ Скасувати"
DONE_TEXT = "✅ Готово"


class ArticleFlow(StatesGroup):
    adding_article = State()
    adding_name = State()
    searching = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


def is_done(text: str | None) -> bool:
    return bool(text) and text.strip() == DONE_TEXT


async def _finish_adding(msg: Message, state: FSMContext):
    """Вихід із циклу додавання з підсумком."""
    fd = await state.get_data()
    added = fd.get("add_count", 0)
    await state.clear()
    text = f"✅ Готово. Додано артикулів: {added}." if added else "Готово."
    await msg.answer(text, reply_markup=kb_main())


@router.callback_query(F.data.startswith("shopart:"))
async def articles_menu_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await cb.message.edit_text("📦 *Артикули товарів*", reply_markup=ikb_articles_menu(shop_id))
        await cb.answer()
    except Exception:
        logger.exception("articles_menu_cb failed")
        await _safe_alert(cb)


# ============================================================
# Список
# ============================================================

@router.callback_query(F.data.startswith("artlist:"))
async def articles_list_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        articles = await articles_db.get_articles(shop_id)
        if not articles:
            text = "📭 Ще немає жодного артикулу в цьому магазині."
        else:
            lines = ["📋 *Артикули магазину:*", ""]
            for a in articles[:100]:
                lines.append(f"• `{a.get('article','')}` — {a.get('product_name','')}")
            text = "\n".join(lines)
        await cb.message.edit_text(text, reply_markup=ikb_articles_menu(shop_id))
        await cb.answer()
    except DBUnavailable:
        await _safe_alert(cb)
    except Exception:
        logger.exception("articles_list_cb failed")
        await _safe_alert(cb)


# ============================================================
# Пошук
# ============================================================

@router.callback_query(F.data.startswith("artsearch:"))
async def article_search_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(ArticleFlow.searching)
        await state.update_data(search_shop_id=shop_id)
        await cb.message.answer(
            f"🔎 Введи артикул для пошуку.\nМожна шукати кілька підряд, вихід — «{DONE_TEXT}».",
            reply_markup=kb_article_more(),
        )
        await cb.answer()
    except Exception:
        logger.exception("article_search_start_cb failed")
        await _safe_alert(cb)


@router.message(ArticleFlow.searching)
async def article_search_run(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    if is_done(msg.text):
        await state.clear()
        return await msg.answer("Готово.", reply_markup=kb_main())

    fd = await state.get_data()
    shop_id = fd.get("search_shop_id")
    article = (msg.text or "").strip()
    if not article:
        return await msg.answer("⚠️ Введи артикул:", reply_markup=kb_article_more())

    try:
        found = await articles_db.find_article(shop_id, article)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    # Лишаємось у режимі пошуку — можна одразу шукати наступний
    if not found:
        return await msg.answer(
            f"📭 Артикул `{article}` в цьому магазині не знайдено.\n"
            f"Введи наступний або натисни «{DONE_TEXT}».",
            reply_markup=kb_article_more(),
        )

    await msg.answer(
        f"✅ Знайдено:\n\n📦 Артикул: `{found.get('article','')}`\n"
        f"📝 Товар: {found.get('product_name','')}\n"
        f"🗓 Додано: {found.get('created_at','')[:16].replace('T',' ')}\n\n"
        f"Введи наступний артикул або натисни «{DONE_TEXT}».",
        reply_markup=kb_article_more(),
    )


# ============================================================
# Додати артикул окремо (не через створення поста)
# ============================================================

@router.callback_query(F.data.startswith("artadd:"))
async def article_add_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(ArticleFlow.adding_article)
        await state.update_data(add_shop_id=shop_id, add_article=None, add_count=0)
        await cb.message.answer("📝 Введи артикул товару:", reply_markup=kb_cancel_article())
        await cb.answer()
    except Exception:
        logger.exception("article_add_start_cb failed")
        await _safe_alert(cb)


@router.message(ArticleFlow.adding_article)
async def article_add_article_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    if is_done(msg.text):
        return await _finish_adding(msg, state)

    article = (msg.text or "").strip()
    if not article:
        return await msg.answer("⚠️ Введи артикул:", reply_markup=kb_cancel_article())

    fd = await state.get_data()
    shop_id = fd.get("add_shop_id")
    if not shop_id:
        await state.clear()
        return await msg.answer(
            "⚠️ Сесія додавання загубилась. Відкрий магазин і почни спочатку.",
            reply_markup=kb_main(),
        )

    try:
        existing = await articles_db.find_article(shop_id, article)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    if existing:
        await state.update_data(add_article=article)
        return await msg.answer(
            f"⚠️ Товар з артикулом {existing.get('article', article)} вже доданий.",
            reply_markup=ikb_article_add_duplicate(shop_id, str(existing["_id"])),
        )

    await state.update_data(add_article=article)
    await state.set_state(ArticleFlow.adding_name)
    await msg.answer("📝 Введи назву товару:", reply_markup=kb_cancel_article())


@router.message(ArticleFlow.adding_name)
async def article_add_name_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    name = (msg.text or "").strip()[:128]
    if not name:
        return await msg.answer("⚠️ Введи назву товару:", reply_markup=kb_cancel_article())

    fd = await state.get_data()
    shop_id = fd.get("add_shop_id")
    article = fd.get("add_article")
    if not shop_id or not article:
        await state.clear()
        return await msg.answer(
            "⚠️ Сесія додавання загубилась. Відкрий магазин і почни спочатку.",
            reply_markup=kb_main(),
        )

    try:
        new_id = await articles_db.add_article(msg.from_user.id, shop_id, article, name)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    # Не чистимо стан — повертаємось у цикл додавання,
    # скидаємо тільки дані поточного артикула.
    await state.update_data(add_article=None)
    await state.set_state(ArticleFlow.adding_article)

    if new_id is None:
        # Хтось встиг додати той самий артикул між перевіркою і збереженням
        # (паралельний запит) — індекс у базі це відловив.
        return await msg.answer(
            f"⚠️ Артикул `{article}` щойно додали паралельно.\n"
            f"Введи наступний або натисни «{DONE_TEXT}».",
            reply_markup=kb_article_more(),
        )

    count = fd.get("add_count", 0) + 1
    await state.update_data(add_count=count)
    await msg.answer(
        f"✅ Артикул `{article}` («{name}») додано. Всього за сесію: {count}.\n\n"
        f"📝 Введи наступний артикул або натисни «{DONE_TEXT}».",
        reply_markup=kb_article_more(),
    )


@router.callback_query(F.data.startswith("artaddview:"))
async def article_add_view_dup_cb(cb: CallbackQuery):
    try:
        article_id = cb.data.split(":", 1)[1]
        a = await articles_db.get_article(article_id)
        if not a:
            return await cb.answer("Товар не знайдено.", show_alert=True)
        await cb.message.answer(
            f"👁 *Існуючий товар:*\n\n📦 Артикул: `{a.get('article','')}`\n"
            f"📝 Назва: {a.get('product_name','')}\n"
            f"🗓 Додано: {a.get('created_at','')[:16].replace('T',' ')}",
        )
        await cb.answer()
    except Exception:
        logger.exception("article_add_view_dup_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("artaddforce:"))
async def article_add_force_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        fd = await state.get_data()
        if not fd.get("add_article") or fd.get("add_shop_id") != shop_id:
            await state.update_data(add_shop_id=shop_id)
        await state.set_state(ArticleFlow.adding_name)
        await cb.message.answer("📝 Введи назву товару:", reply_markup=kb_cancel_article())
        await cb.answer()
    except Exception:
        logger.exception("article_add_force_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("artaddcancel:"))
async def article_add_cancel_cb(cb: CallbackQuery, state: FSMContext):
    """Відмова від дубля: не рвемо весь цикл, якщо він ще активний."""
    try:
        current = await state.get_state()
        if current in (ArticleFlow.adding_article.state, ArticleFlow.adding_name.state):
            await state.update_data(add_article=None)
            await state.set_state(ArticleFlow.adding_article)
            await cb.message.answer(
                f"Пропущено. Введи інший артикул або натисни «{DONE_TEXT}».",
                reply_markup=kb_article_more(),
            )
        else:
            await state.clear()
            await cb.message.answer("❌ Скасовано.", reply_markup=kb_main())
        await cb.answer()
    except Exception:
        logger.exception("article_add_cancel_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass