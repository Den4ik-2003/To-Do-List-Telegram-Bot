import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import shops as shops_db
from database import shop_templates as templates_db
from database import shop_posts as posts_db
from services import channel_service
from services.post_template_service import render_template, extract_fields
from keyboards.main_menu import kb_main
from keyboards.posts import (
    kb_cancel_post, kb_photos_step, ikb_shop_pick, ikb_template_pick,
    ikb_post_preview,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="posts")

CANCEL_TEXT = "❌ Скасувати"
DONE_TEXT = "✅ Готово"
NO_PHOTO_TEXT = "⏭ Без фото"


class CreatePost(StatesGroup):
    choosing_shop = State()
    choosing_template = State()
    waiting_text = State()
    waiting_photos = State()
    waiting_missing_field = State()
    editing_text = State()


class QuickPost(StatesGroup):
    choosing_shop = State()
    waiting_photos = State()
    waiting_text = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


async def _cancel_to_main(msg: Message, state: FSMContext):
    fd = await state.get_data()
    draft_id = fd.get("post_draft_id")
    if draft_id:
        await posts_db.delete_draft(draft_id)
    await state.clear()
    await msg.answer("Скасовано.", reply_markup=kb_main())


@router.message(F.text == "📢 Створити пост")
async def create_post_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        shops = await shops_db.get_shops(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not shops:
        return await msg.answer("📭 Спочатку додай магазин через «🏪 Мої магазини».", reply_markup=kb_main())
    await state.set_state(CreatePost.choosing_shop)
    await msg.answer("🏪 Обери магазин:", reply_markup=ikb_shop_pick(shops, "postshop"))


@router.callback_query(CreatePost.choosing_shop, F.data.startswith("postshop:"))
async def create_post_shop_chosen(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        templates = await templates_db.get_templates(shop_id)
        if not templates:
            await state.clear()
            await cb.message.edit_text("📭 У цього магазину ще немає шаблонів. Додай шаблон через «🏪 Мої магазини».")
            return await cb.answer()
        await state.update_data(post_shop_id=shop_id)
        await state.set_state(CreatePost.choosing_template)
        await cb.message.edit_text("📂 Обери шаблон:", reply_markup=ikb_template_pick(templates, "posttpl"))
        await cb.answer()
    except Exception:
        logger.exception("create_post_shop_chosen failed")
        await _safe_alert(cb)


@router.callback_query(CreatePost.choosing_template, F.data.startswith("posttpl:"))
async def create_post_template_chosen(cb: CallbackQuery, state: FSMContext):
    try:
        template_id = cb.data.split(":", 1)[1]
        await state.update_data(post_template_id=template_id)
        await state.set_state(CreatePost.waiting_text)
        await cb.message.edit_text("📝 Надішли текст для поста:")
        await cb.answer()
    except Exception:
        logger.exception("create_post_template_chosen failed")
        await _safe_alert(cb)


@router.message(CreatePost.waiting_text)
async def create_post_text_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстове повідомлення:", reply_markup=kb_cancel_post())
    await state.update_data(post_raw_text=msg.text, post_photos=[])
    await state.set_state(CreatePost.waiting_photos)
    await msg.answer(
        "📸 Надішли фото (одне або кілька, по одному), або «⏭ Без фото». "
        "Коли завершиш — натисни «✅ Готово»:",
        reply_markup=kb_photos_step(),
    )


@router.message(CreatePost.waiting_photos, F.photo)
async def create_post_photo_received(msg: Message, state: FSMContext):
    fd = await state.get_data()
    photos = fd.get("post_photos", [])
    photos.append(msg.photo[-1].file_id)
    await state.update_data(post_photos=photos)
    await msg.answer(f"✅ Додано фото ({len(photos)}). Ще фото або натисни «✅ Готово».")


@router.message(CreatePost.waiting_photos)
async def create_post_photos_done(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if msg.text not in (DONE_TEXT, NO_PHOTO_TEXT):
        return await msg.answer("⚠️ Надішли фото, натисни «✅ Готово» або «⏭ Без фото»:", reply_markup=kb_photos_step())
    await _proceed_to_render(msg, state)


async def _proceed_to_render(msg: Message, state: FSMContext):
    fd = await state.get_data()
    template_id = fd["post_template_id"]
    template = await templates_db.get_template(template_id)
    if not template:
        await state.clear()
        return await msg.answer("⚠️ Шаблон не знайдено, спробуй ще раз.", reply_markup=kb_main())

    fields = await extract_fields(template["text_template"], template["placeholders"], fd["post_raw_text"])
    missing = [name for name, val in fields.items() if not val]
    await state.update_data(post_fields=fields, post_missing=missing)

    if missing:
        await state.set_state(CreatePost.waiting_missing_field)
        return await msg.answer(f"✏️ Не вистачає значення для «{missing[0]}». Введи його:", reply_markup=kb_cancel_post())

    await _show_preview(msg, state)


@router.message(CreatePost.waiting_missing_field)
async def create_post_missing_field(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if not msg.text:
        return await msg.answer("⚠️ Введи значення текстом:", reply_markup=kb_cancel_post())

    fd = await state.get_data()
    fields = fd["post_fields"]
    missing = fd["post_missing"]
    current = missing.pop(0)
    fields[current] = msg.text.strip()
    await state.update_data(post_fields=fields, post_missing=missing)

    if missing:
        await state.set_state(CreatePost.waiting_missing_field)
        return await msg.answer(f"✏️ Не вистачає значення для «{missing[0]}». Введи його:", reply_markup=kb_cancel_post())

    await _show_preview(msg, state)


async def _show_preview(msg: Message, state: FSMContext):
    fd = await state.get_data()
    template_id = fd["post_template_id"]
    shop_id = fd["post_shop_id"]
    template = await templates_db.get_template(template_id)
    shop = await shops_db.get_shop(shop_id)
    if not template or not shop:
        await state.clear()
        return await msg.answer("⚠️ Дані застаріли, почни спочатку.", reply_markup=kb_main())

    rendered_text = render_template(template["text_template"], fd["post_fields"])
    photos = fd.get("post_photos", [])
    sticker_file_id = template.get("opening_sticker_file_id")

    draft_id = await posts_db.save_draft(
        msg.from_user.id, shop_id, template_id, fd["post_fields"], rendered_text, photos, sticker_file_id,
    )
    await state.update_data(post_rendered_text=rendered_text, post_draft_id=draft_id)
    await state.set_state(None)

    await msg.answer("👀 *Прев'ю поста:*", reply_markup=kb_main())
    await channel_service.publish_post(msg.bot, msg.from_user.id, rendered_text, photos, sticker_file_id)
    await msg.answer("Що робимо далі?", reply_markup=ikb_post_preview(has_channel=bool(shop.get("channel_id"))))


@router.callback_query(F.data == "postedittext")
async def post_edit_text_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CreatePost.editing_text)
    await cb.message.answer("✏️ Надішли новий текст для поста:", reply_markup=kb_cancel_post())
    await cb.answer()


@router.message(CreatePost.editing_text)
async def post_edit_text_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстове повідомлення:", reply_markup=kb_cancel_post())
    await state.update_data(post_raw_text=msg.text)
    await _proceed_to_render(msg, state)


@router.callback_query(F.data == "postretpl")
async def post_retemplate_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    shop_id = fd.get("post_shop_id")
    templates = await templates_db.get_templates(shop_id)
    await state.set_state(CreatePost.choosing_template)
    await cb.message.answer("📂 Обери інший шаблон:", reply_markup=ikb_template_pick(templates, "posttpl"))
    await cb.answer()


@router.callback_query(F.data == "postcancel")
async def post_cancel_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    draft_id = fd.get("post_draft_id")
    if draft_id:
        await posts_db.delete_draft(draft_id)
    await state.clear()
    await cb.message.edit_text("❌ Скасовано.")
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())
    await cb.answer()


@router.callback_query(F.data == "postpublish")
async def post_publish_cb(cb: CallbackQuery, state: FSMContext):
    try:
        fd = await state.get_data()
        shop_id = fd.get("post_shop_id")
        template_id = fd.get("post_template_id")
        rendered_text = fd.get("post_rendered_text")
        photos = fd.get("post_photos", [])
        draft_id = fd.get("post_draft_id")
        shop = await shops_db.get_shop(shop_id)
        template = await templates_db.get_template(template_id)
        if not shop or not shop.get("channel_id"):
            return await cb.answer("Канал не підключено.", show_alert=True)

        sticker_file_id = template.get("opening_sticker_file_id") if template else None
        message_ids = await channel_service.publish_post(
            cb.bot, shop["channel_id"], rendered_text, photos, sticker_file_id,
        )
        await posts_db.add_published_post(cb.from_user.id, shop_id, shop["channel_id"], message_ids, rendered_text, photos)
        await shops_db.set_last_template(shop_id, template_id)
        if draft_id:
            await posts_db.delete_draft(draft_id)

        await state.clear()
        await cb.message.edit_text("🚀 Пост опубліковано в каналі!")
        await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())
        await cb.answer()
    except Exception:
        logger.exception("post_publish_cb failed")
        await cb.answer("⚠️ Не вдалося опублікувати. Перевір права бота в каналі.", show_alert=True)


@router.message(F.text == "⚡ Швидкий пост")
async def quick_post_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        shops = [s for s in await shops_db.get_shops(msg.from_user.id) if s.get("last_template_id")]
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not shops:
        return await msg.answer(
            "📭 Немає магазину з готовим шаблоном. Спочатку створи магазин і шаблон.",
            reply_markup=kb_main(),
        )
    await state.set_state(QuickPost.choosing_shop)
    await msg.answer("🏪 Обери магазин:", reply_markup=ikb_shop_pick(shops, "qshop"))


@router.callback_query(QuickPost.choosing_shop, F.data.startswith("qshop:"))
async def quick_post_shop_chosen(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.update_data(post_shop_id=shop_id, post_photos=[])
        await state.set_state(QuickPost.waiting_photos)
        await cb.message.edit_text(
            "📸 Надішли фото (одне або кілька), або «⏭ Без фото». Коли завершиш — «✅ Готово»:"
        )
        await cb.message.answer("Керування нижче:", reply_markup=kb_photos_step())
        await cb.answer()
    except Exception:
        logger.exception("quick_post_shop_chosen failed")
        await _safe_alert(cb)


@router.message(QuickPost.waiting_photos, F.photo)
async def quick_post_photo_received(msg: Message, state: FSMContext):
    fd = await state.get_data()
    photos = fd.get("post_photos", [])
    photos.append(msg.photo[-1].file_id)
    await state.update_data(post_photos=photos)
    if msg.caption:
        await state.update_data(post_raw_text=msg.caption)
    await msg.answer(f"✅ Додано фото ({len(photos)}). Ще фото або натисни «✅ Готово».")


@router.message(QuickPost.waiting_photos)
async def quick_post_photos_done(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if msg.text in (DONE_TEXT, NO_PHOTO_TEXT):
        fd = await state.get_data()
        if fd.get("post_raw_text"):
            shop_id = fd["post_shop_id"]
            shop = await shops_db.get_shop(shop_id)
            await state.update_data(post_template_id=shop["last_template_id"])
            await _proceed_to_render(msg, state)
            return
        await state.set_state(QuickPost.waiting_text)
        return await msg.answer("📝 Надішли текст для поста:", reply_markup=kb_cancel_post())
    return await msg.answer("⚠️ Надішли фото, натисни «✅ Готово» або «⏭ Без фото»:", reply_markup=kb_photos_step())


@router.message(QuickPost.waiting_text)
async def quick_post_text_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        return await _cancel_to_main(msg, state)
    if not msg.text:
        return await msg.answer("⚠️ Надішли текстове повідомлення:", reply_markup=kb_cancel_post())
    fd = await state.get_data()
    shop_id = fd["post_shop_id"]
    shop = await shops_db.get_shop(shop_id)
    await state.update_data(post_raw_text=msg.text, post_template_id=shop["last_template_id"])
    await _proceed_to_render(msg, state)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass