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
from keyboards.shops import ikb_articles_menu, kb_cancel_article

logger = logging.getLogger("tasks_bot")
router = Router(name="shop_articles")

CANCEL_TEXT = "❌ Скасувати"


class ArticleFlow(StatesGroup):
    searching = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


@router.callback_query(F.data.startswith("shopart:"))
async def articles_menu_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await cb.message.edit_text("📦 *Артикули товарів*", reply_markup=ikb_articles_menu(shop_id))
        await cb.answer()
    except Exception:
        logger.exception("articles_menu_cb failed")
        await _safe_alert(cb)


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


@router.callback_query(F.data.startswith("artsearch:"))
async def article_search_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(ArticleFlow.searching)
        await state.update_data(search_shop_id=shop_id)
        await cb.message.answer("🔎 Введи артикул для пошуку:", reply_markup=kb_cancel_article())
        await cb.answer()
    except Exception:
        logger.exception("article_search_start_cb failed")
        await _safe_alert(cb)


@router.message(ArticleFlow.searching)
async def article_search_run(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    fd = await state.get_data()
    shop_id = fd.get("search_shop_id")
    article = (msg.text or "").strip()
    await state.clear()
    try:
        found = await articles_db.find_article(shop_id, article)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not found:
        return await msg.answer(f"📭 Артикул `{article}` в цьому магазині не знайдено.", reply_markup=kb_main())
    await msg.answer(
        f"✅ Знайдено:\n\n📦 Артикул: `{found.get('article','')}`\n📝 Товар: {found.get('product_name','')}\n"
        f"🗓 Додано: {found.get('created_at','')[:16].replace('T',' ')}",
        reply_markup=kb_main(),
    )


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass