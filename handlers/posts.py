import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import shops as shops_db
from database import shop_content as content_db
from database import shop_articles as articles_db
from database import shop_posts as posts_db
from services.post_template_service import render_template, extract_fields
from keyboards.main_menu import kb_main
from keyboards.posts import (
    kb_cancel_post, kb_photos_step, ikb_shop_pick, ikb_template_pick,
    ikb_post_preview, ikb_article_duplicate,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="posts")

CANCEL_TEXT = "❌ Скасувати"
DONE_TEXT = "✅ Готово"
NO_PHOTO_TEXT = "⏭ Без фото"


class PostFlow(StatesGroup):
    waiting_product_name = State()
    waiting_article = State()
    waiting_photos = State()
    waiting_text = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


async def _reset(state: FSMContext):
    await state.clear()


# ============================================================
# Старт: вибір магазину
# ============================================================

@router.message(F.text == "📢 Створити пост")
async def post_start(msg: Message, state: FSMContext):
    try:
        shops = await shops_db.get_shops(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not shops:
        return await msg.answer("📭 Спочатку додай магазин («🏪 Мої магазини»).", reply_markup=kb_main())
    await msg.answer("🏪 Обери магазин:", reply_markup=ikb_shop_pick(shops, "postshop"))


@router.callback_query(F.data.startswith("postshop:"))
async def post_shop_chosen_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(PostFlow.waiting_product_name)
        await state.update_data(post_shop_id=shop_id)
        await cb.message.answer("📦 Введи назву товару:", reply_markup=kb_cancel_post())
        await cb.answer()
    except Exception:
        logger.exception("post_shop_chosen_cb failed")
        await _safe_alert(cb)


# ============================================================
# Швидкий пост: одразу з останнім шаблоном магазину
# ============================================================

@router.message(F.text == "⚡ Швидкий пост")
async def quick_post_start(msg: Message, state: FSMContext):
    try:
        shops = await shops_db.get_shops(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not shops:
        return await msg.answer("📭 Спочатку додай магазин («🏪 Мої магазини»).", reply_markup=kb_main())
    await msg.answer("🏪 Обери магазин для швидкого поста:", reply_markup=ikb_shop_pick(shops, "postquickshop"))


@router.callback_query(F.data.startswith("postquickshop:"))
async def quick_post_shop_chosen_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        shop = await shops_db.get_shop(shop_id)
        if not shop or not shop.get("last_template_id"):
            await cb.answer()
            return await cb.message.answer(
                "⚠️ У цього магазину ще немає жодного шаблону. Спочатку створи шаблон.",
                reply_markup=kb_main(),
            )
        template = await content_db.get_template(shop["last_template_id"])
        if not template:
            await cb.answer()
            return await cb.message.answer("⚠️ Останній шаблон не знайдено, обери шаблон вручну через «📢 Створити пост».", reply_markup=kb_main())
        template = content_db.migrate_template(template)
        await state.set_state(PostFlow.waiting_text)
        await state.update_data(
            post_shop_id=shop_id, post_product_name=None, post_article=None, post_article_id=None,
            post_template_id=str(template["_id"]), post_template=template, post_photos=[],
        )
        await cb.message.answer(
            "✏️ Надішли текст товару — підставлю в останній використаний шаблон "
            f"«{template.get('name','')}»:",
            reply_markup=kb_cancel_post(),
        )
        await cb.answer()
    except Exception:
        logger.exception("quick_post_shop_chosen_cb failed")
        await _safe_alert(cb)


# ============================================================
# Застосувати конкретний шаблон одразу з екрана шаблону
# ============================================================

@router.callback_query(F.data.startswith("posttplquick:"))
async def post_template_quick_cb(cb: CallbackQuery, state: FSMContext):
    try:
        _, shop_id, template_id = cb.data.split(":")
        template = await content_db.get_template(template_id)
        if not template:
            return await cb.answer("Шаблон не знайдено.", show_alert=True)
        template = content_db.migrate_template(template)
        await state.set_state(PostFlow.waiting_text)
        await state.update_data(
            post_shop_id=shop_id, post_product_name=None, post_article=None, post_article_id=None,
            post_template_id=template_id, post_template=template, post_photos=[],
        )
        await cb.message.answer(
            f"✏️ Надішли текст товару — підставлю в шаблон «{template.get('name','')}»:",
            reply_markup=kb_cancel_post(),
        )
        await cb.answer()
    except Exception:
        logger.exception("post_template_quick_cb failed")
        await _safe_alert(cb)


# ============================================================
# Товар → Артикул
# ============================================================

@router.message(PostFlow.waiting_product_name)
async def post_product_name_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await _reset(state)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    name = (msg.text or "").strip()[:128]
    if not name:
        return await msg.answer("⚠️ Введи назву товару:", reply_markup=kb_cancel_post())
    await state.update_data(post_product_name=name)
    await state.set_state(PostFlow.waiting_article)
    await msg.answer("📝 Введи артикул товару:", reply_markup=kb_cancel_post())


@router.message(PostFlow.waiting_article)
async def post_article_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await _reset(state)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    article = (msg.text or "").strip()
    if not article:
        return await msg.answer("⚠️ Введи артикул:", reply_markup=kb_cancel_post())

    fd = await state.get_data()
    shop_id = fd["post_shop_id"]

    try:
        existing = await articles_db.find_article(shop_id, article)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    await state.update_data(post_article=article)

    if existing:
        await state.update_data(post_article_pending_duplicate=True)
        return await msg.answer(
            f"⚠️ Товар з артикулом {existing.get('article', article)} вже доданий.",
            reply_markup=ikb_article_duplicate(str(existing["_id"])),
        )

    await state.update_data(post_article_pending_duplicate=False)
    await _go_to_template_pick(msg, state)


@router.callback_query(F.data.startswith("artview:"))
async def post_article_view_dup_cb(cb: CallbackQuery, state: FSMContext):
    try:
        article_id = cb.data.split(":", 1)[1]
        a = await articles_db.get_article(article_id)
        if not a:
            return await cb.answer("Товар не знайдено.", show_alert=True)
        await cb.message.answer(
            f"👁 *Існуючий товар:*\n\n📦 Артикул: `{a.get('article','')}`\n📝 Назва: {a.get('product_name','')}\n"
            f"🗓 Додано: {a.get('created_at','')[:16].replace('T',' ')}\n\n"
            "Натисни «🔄 Все одно додати», якщо хочеш продовжити з тим самим артикулом, або «❌ Скасувати».",
        )
        await cb.answer()
    except Exception:
        logger.exception("post_article_view_dup_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "artforce")
async def post_article_force_cb(cb: CallbackQuery, state: FSMContext):
    try:
        await state.update_data(post_article_pending_duplicate=False, post_article_force=True)
        await cb.answer()
        await _go_to_template_pick(cb.message, state, edit=False)
    except Exception:
        logger.exception("post_article_force_cb failed")
        await _safe_alert(cb)


async def _go_to_template_pick(msg: Message, state: FSMContext, edit: bool = False):
    fd = await state.get_data()
    shop_id = fd["post_shop_id"]
    try:
        templates = await content_db.get_templates(shop_id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    templates = [content_db.migrate_template(t) for t in templates]
    if not templates:
        await state.set_state(PostFlow.waiting_text)
        await state.update_data(post_template_id=None, post_template=None, post_photos=[])
        return await msg.answer(
            "⚠️ У цього магазину ще немає шаблонів — надішли текст товару, опублікую без форматування:",
            reply_markup=kb_cancel_post(),
        )
    await msg.answer("📝 Обери шаблон:", reply_markup=ikb_template_pick(templates, "posttpl"))


@router.callback_query(F.data.startswith("posttpl:"))
async def post_template_chosen_cb(cb: CallbackQuery, state: FSMContext):
    try:
        template_id = cb.data.split(":", 1)[1]
        template = await content_db.get_template(template_id)
        if not template:
            return await cb.answer("Шаблон не знайдено.", show_alert=True)
        template = content_db.migrate_template(template)
        await state.update_data(post_template_id=template_id, post_template=template, post_photos=[])
        await state.set_state(PostFlow.waiting_photos)
        await cb.message.answer(
            "📸 Надішли фото товару (можна кілька), потім натисни «✅ Готово», або «⏭ Без фото»:",
            reply_markup=kb_photos_step(),
        )
        await cb.answer()
    except Exception:
        logger.exception("post_template_chosen_cb failed")
        await _safe_alert(cb)


# ============================================================
# Фото → Текст
# ============================================================

@router.message(PostFlow.waiting_photos, F.photo)
async def post_photo_received(msg: Message, state: FSMContext):
    fd = await state.get_data()
    photos = fd.get("post_photos", [])
    photos.append(msg.photo[-1].file_id)
    await state.update_data(post_photos=photos)
    await msg.answer(f"📸 Додано ({len(photos)}). Ще фото, або «✅ Готово» / «⏭ Без фото»:", reply_markup=kb_photos_step())


@router.message(PostFlow.waiting_photos)
async def post_photos_step_text(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await _reset(state)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    if msg.text not in (DONE_TEXT, NO_PHOTO_TEXT):
        return await msg.answer("⚠️ Надішли фото, або натисни «✅ Готово» / «⏭ Без фото»:", reply_markup=kb_photos_step())
    await state.set_state(PostFlow.waiting_text)
    await msg.answer("✏️ Надішли текст товару (я підставлю дані в шаблон):", reply_markup=kb_cancel_post())


@router.message(PostFlow.waiting_text)
async def post_text_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await _reset(state)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    user_text = msg.text or ""
    if not user_text.strip():
        return await msg.answer("⚠️ Надішли текст товару:", reply_markup=kb_cancel_post())

    fd = await state.get_data()
    shop_id = fd["post_shop_id"]
    template = fd.get("post_template")

    if template:
        # AI визначає ЛИШЕ дані для плейсхолдерів. Форматування завжди керується
        # шаблоном, а не генерується заново — навіть якщо AI недоступний,
        # extract_fields() відкатується на _fallback_extract() і базове
        # застосування шаблону все одно працює.
        fields = await extract_fields(template["text_template"], template["placeholders"], user_text)
        rendered_text = render_template(template["text_template"], fields)
    else:
        fields = {}
        rendered_text = user_text

    await state.update_data(post_fields=fields, post_rendered_text=rendered_text)
    await _show_preview(msg, state)


async def _show_preview(msg: Message, state: FSMContext):
    fd = await state.get_data()
    shop_id = fd["post_shop_id"]
    rendered_text = fd["post_rendered_text"]
    photos = fd.get("post_photos", [])
    shop = await shops_db.get_shop(shop_id)
    has_channel = bool(shop and shop.get("channel_id"))

    preview_text = f"👁 *Прев'ю поста*\n\n{rendered_text}"
    if photos:
        from aiogram.types import InputMediaPhoto
        media = [InputMediaPhoto(media=fid) for fid in photos]
        media[0].caption = preview_text
        media[0].parse_mode = "Markdown"
        await msg.answer_media_group(media)
        await msg.answer("Дії з постом:", reply_markup=ikb_post_preview(has_channel))
    else:
        await msg.answer(preview_text, reply_markup=ikb_post_preview(has_channel))


@router.callback_query(F.data == "postedittext")
async def post_edit_text_cb(cb: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(PostFlow.waiting_text)
        await cb.message.answer("✏️ Надішли новий текст товару:", reply_markup=kb_cancel_post())
        await cb.answer()
    except Exception:
        logger.exception("post_edit_text_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "postretpl")
async def post_retemplate_cb(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.answer()
        await _go_to_template_pick(cb.message, state)
    except Exception:
        logger.exception("post_retemplate_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data == "postcancel")
async def post_cancel_cb(cb: CallbackQuery, state: FSMContext):
    try:
        await _reset(state)
        await cb.message.answer("❌ Скасовано.", reply_markup=kb_main())
        await cb.answer()
    except Exception:
        logger.exception("post_cancel_cb failed")
        await _safe_alert(cb)


# ============================================================
# Публікація
# ============================================================

@router.callback_query(F.data == "postpublish")
async def post_publish_cb(cb: CallbackQuery, state: FSMContext, bot):
    try:
        fd = await state.get_data()
        shop_id = fd["post_shop_id"]
        rendered_text = fd["post_rendered_text"]
        photos = fd.get("post_photos", [])
        template = fd.get("post_template")
        template_id = fd.get("post_template_id")
        article = fd.get("post_article")
        product_name = fd.get("post_product_name")

        shop = await shops_db.get_shop(shop_id)
        if not shop or not shop.get("channel_id"):
            return await cb.answer("У магазину немає підключеного каналу.", show_alert=True)
        channel_id = shop["channel_id"]

        # Зберігаємо артикул саме зараз (успішний момент публікації) —
        # якщо він новий, або юзер підтвердив дублікат через "artforce".
        article_id = None
        if article:
            existing = await articles_db.find_article(shop_id, article)
            if existing:
                article_id = str(existing["_id"])
            else:
                new_id = await articles_db.add_article(cb.from_user.id, shop_id, article, product_name or "")
                article_id = new_id

        message_ids = []

        sticker_id = (template or {}).get("opening_sticker_file_id")
        if sticker_id:
            sent = await bot.send_sticker(channel_id, sticker_id)
            message_ids.append(sent.message_id)

        if photos:
            from aiogram.types import InputMediaPhoto
            media = [InputMediaPhoto(media=fid) for fid in photos]
            media[0].caption = rendered_text
            media[0].parse_mode = "Markdown"
            sent_msgs = await bot.send_media_group(channel_id, media)
            message_ids.extend(m.message_id for m in sent_msgs)
        else:
            sent = await bot.send_message(channel_id, rendered_text, parse_mode="Markdown")
            message_ids.append(sent.message_id)

        await posts_db.add_published_post(
            cb.from_user.id, shop_id, channel_id, message_ids, rendered_text, photos,
            template_id=template_id, article_id=article_id,
        )
        if template_id:
            await shops_db.set_last_template(shop_id, template_id)

        await _reset(state)
        await cb.message.answer("🚀 Опубліковано!", reply_markup=kb_main())
        await cb.answer()
    except DBUnavailable:
        await _safe_alert(cb)
    except TelegramAPIError:
        logger.exception("post_publish_cb telegram send failed")
        await cb.answer("⚠️ Не вдалося опублікувати в канал. Перевір права бота.", show_alert=True)
    except Exception:
        logger.exception("post_publish_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass