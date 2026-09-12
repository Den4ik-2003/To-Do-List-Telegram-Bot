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
from services import ai_service
from services.post_template_service import analyze_example, extract_placeholders
from keyboards.main_menu import kb_main
from keyboards.shop_templates import (
    kb_cancel_tpl, kb_sticker_step, ikb_templates_list, ikb_template_actions,
    ikb_template_confirm, ikb_template_delete_confirm,
)

logger = logging.getLogger("tasks_bot")
router = Router(name="shop_templates")

CANCEL_TEXT = "❌ Скасувати"
NO_STICKER_TEXT = "⏭ Без стікера"


class TemplateFlow(StatesGroup):
    waiting_example = State()
    waiting_manual_text = State()
    waiting_sticker = State()
    waiting_name = State()
    editing_text = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


@router.callback_query(F.data.startswith("shoptpls:"))
async def templates_list_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        templates = await content_db.get_templates(shop_id)
        templates = [content_db.migrate_template(t) for t in templates]
        text = "📂 *Шаблони магазину*"
        if not templates:
            text += "\n\n📭 Ще немає жодного шаблону."
        await cb.message.edit_text(text, reply_markup=ikb_templates_list(shop_id, templates))
        await cb.answer()
    except Exception:
        logger.exception("templates_list_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("tpladd:"))
async def template_add_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(TemplateFlow.waiting_example)
        await state.update_data(tpl_shop_id=shop_id)
        await cb.message.answer(
            "📝 Надішли приклад готового поста — текст, який задає стиль оформлення "
            "(емодзі, переноси рядків, порядок). Наприклад:\n\n"
            "🔥 CASIO\n\n🖤 Чорний колір\n\n💰 2500 грн",
            reply_markup=kb_cancel_tpl(),
        )
        await cb.answer()
    except Exception:
        logger.exception("template_add_start_cb failed")
        await _safe_alert(cb)


@router.message(TemplateFlow.waiting_example)
async def template_example_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw_text = msg.text or ""
    if not raw_text.strip():
        return await msg.answer("⚠️ Надішли текстовий приклад поста:", reply_markup=kb_cancel_tpl())

    await state.update_data(tpl_raw_example=raw_text)

    if not ai_service.is_available():
        await state.set_state(TemplateFlow.waiting_manual_text)
        return await msg.answer(
            "⚠️ AI зараз недоступний, тому онови текст вручну: перепиши цей приклад, "
            "замінивши змінні частини (назву, колір, ціну тощо) на плейсхолдери у форматі "
            "{{назва_поля}}, наприклад:\n\n🔥 {{product}}\n\n🖤 {{color}}\n\n💰 {{price}} грн",
            reply_markup=kb_cancel_tpl(),
        )

    wait_msg = await msg.answer("🤖 Аналізую структуру прикладу...")
    result = await analyze_example(raw_text)
    if not result:
        await state.set_state(TemplateFlow.waiting_manual_text)
        return await wait_msg.edit_text(
            "⚠️ Не вдалося автоматично розібрати структуру. Онови текст вручну: заміни змінні "
            "частини на {{назва_поля}}, наприклад:\n\n🔥 {{product}}\n\n🖤 {{color}}\n\n💰 {{price}} грн"
        )

    await state.update_data(tpl_text_template=result["text_template"], tpl_placeholders=result["placeholders"])
    fd = await state.get_data()
    preview = result["text_template"].replace("{{", "❪").replace("}}", "❫")
    await wait_msg.edit_text(
        f"📋 *Пропонований шаблон:*\n\n{preview}\n\n"
        f"Поля: {', '.join(result['placeholders'])}\n\n"
        f"Підходить?",
        reply_markup=ikb_template_confirm(fd["tpl_shop_id"]),
    )


@router.message(TemplateFlow.waiting_manual_text)
async def template_manual_text_received(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    text_template = msg.text or ""
    placeholders = extract_placeholders(text_template)
    if not placeholders:
        return await msg.answer(
            "⚠️ У тексті немає жодного плейсхолдера {{...}}. Додай хоча б один, наприклад {{product}}:",
            reply_markup=kb_cancel_tpl(),
        )
    await state.update_data(tpl_text_template=text_template, tpl_placeholders=placeholders)
    await state.set_state(TemplateFlow.waiting_sticker)
    await msg.answer(
        "🏷 Хочеш прикріпити Telegram-стікер, який надсилатиметься перед постом? "
        "Надішли стікер або натисни «⏭ Без стікера»:",
        reply_markup=kb_sticker_step(),
    )


@router.callback_query(F.data == "tplmanual")
async def template_switch_to_manual_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    text_template = fd.get("tpl_text_template", fd.get("tpl_raw_example", ""))
    await state.set_state(TemplateFlow.waiting_manual_text)
    await cb.message.answer(
        f"✏️ Онови текст шаблону нижче (заміни змінні частини на {{{{назва_поля}}}}):\n\n{text_template}",
        reply_markup=kb_cancel_tpl(),
    )
    await cb.answer()


@router.callback_query(F.data == "tplconfirm")
async def template_confirm_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TemplateFlow.waiting_sticker)
    await cb.message.answer(
        "🏷 Хочеш прикріпити Telegram-стікер, який надсилатиметься перед постом? "
        "Надішли стікер або натисни «⏭ Без стікера»:",
        reply_markup=kb_sticker_step(),
    )
    await cb.answer()


@router.message(TemplateFlow.waiting_sticker, F.sticker)
async def template_sticker_received(msg: Message, state: FSMContext):
    await state.update_data(tpl_sticker_file_id=msg.sticker.file_id)
    await state.set_state(TemplateFlow.waiting_name)
    await msg.answer("📝 Як назвемо цей шаблон?", reply_markup=kb_cancel_tpl())


@router.message(TemplateFlow.waiting_sticker)
async def template_sticker_skip(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    if msg.text != NO_STICKER_TEXT:
        return await msg.answer("⚠️ Надішли стікер або натисни «⏭ Без стікера»:", reply_markup=kb_sticker_step())
    await state.update_data(tpl_sticker_file_id=None)
    await state.set_state(TemplateFlow.waiting_name)
    await msg.answer("📝 Як назвемо цей шаблон?", reply_markup=kb_cancel_tpl())


@router.message(TemplateFlow.waiting_name)
async def template_name_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    name = (msg.text or "").strip()[:64]
    if not name:
        return await msg.answer("⚠️ Введи назву шаблону:", reply_markup=kb_cancel_tpl())

    fd = await state.get_data()
    shop_id = fd["tpl_shop_id"]
    text_template = fd["tpl_text_template"]
    placeholders = fd["tpl_placeholders"]
    sticker_file_id = fd.get("tpl_sticker_file_id")
    raw_example = fd.get("tpl_raw_example", text_template)
    await state.clear()

    try:
        template_id = await content_db.add_template(
            msg.from_user.id, shop_id, name, text_template, placeholders, sticker_file_id,
        )
        await content_db.add_example(msg.from_user.id, shop_id, template_id, name, raw_example, [])
        await shops_db.set_last_template(shop_id, template_id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    await msg.answer(f"✅ Шаблон «{name}» збережено.", reply_markup=kb_main())


@router.callback_query(F.data.startswith("tplopen:"))
async def template_open_cb(cb: CallbackQuery):
    try:
        template_id = cb.data.split(":", 1)[1]
        t = await content_db.get_template(template_id)
        if not t:
            return await cb.answer("Шаблон не знайдено.", show_alert=True)
        t = content_db.migrate_template(t)
        preview = t["text_template"].replace("{{", "❪").replace("}}", "❫")
        text = f"📄 *{t.get('name','')}*\n\n{preview}\n\nПоля: {', '.join(t.get('placeholders', []))}"
        await cb.message.edit_text(text, reply_markup=ikb_template_actions(template_id, t["shop_id"]))
        await cb.answer()
    except Exception:
        logger.exception("template_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("tpledit:"))
async def template_edit_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        template_id = cb.data.split(":", 1)[1]
        t = await content_db.get_template(template_id)
        if not t:
            return await cb.answer("Шаблон не знайдено.", show_alert=True)
        t = content_db.migrate_template(t)
        await state.set_state(TemplateFlow.editing_text)
        await state.update_data(edit_template_id=template_id)
        await cb.message.answer(
            f"✏️ Поточний текст шаблону:\n\n{t['text_template']}\n\n"
            "Надішли новий текст (з {{плейсхолдерами}}):",
            reply_markup=kb_cancel_tpl(),
        )
        await cb.answer()
    except Exception:
        logger.exception("template_edit_start_cb failed")
        await _safe_alert(cb)


@router.message(TemplateFlow.editing_text)
async def template_edit_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    text_template = msg.text or ""
    placeholders = extract_placeholders(text_template)
    if not placeholders:
        return await msg.answer("⚠️ Додай хоча б один {{плейсхолдер}}:", reply_markup=kb_cancel_tpl())

    fd = await state.get_data()
    template_id = fd["edit_template_id"]
    await state.clear()
    try:
        await content_db.update_template(template_id, {"text_template": text_template, "placeholders": placeholders})
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer("✅ Шаблон оновлено.", reply_markup=kb_main())


@router.callback_query(F.data.startswith("tpldel:"))
async def template_delete_ask_cb(cb: CallbackQuery):
    try:
        template_id = cb.data.split(":", 1)[1]
        t = await content_db.get_template(template_id)
        if not t:
            return await cb.answer("Шаблон не знайдено.", show_alert=True)
        await cb.message.edit_text(
            f"🗑 Видалити шаблон «{t.get('name','')}»?",
            reply_markup=ikb_template_delete_confirm(template_id, t["shop_id"]),
        )
        await cb.answer()
    except Exception:
        logger.exception("template_delete_ask_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("tpldelconfirm:"))
async def template_delete_confirm_cb(cb: CallbackQuery):
    try:
        _, template_id, shop_id = cb.data.split(":")
        await content_db.delete_template(template_id)
        templates = await content_db.get_templates(shop_id)
        text = "📂 *Шаблони магазину*"
        if not templates:
            text += "\n\n📭 Ще немає жодного шаблону."
        await cb.message.edit_text(text, reply_markup=ikb_templates_list(shop_id, templates))
        await cb.answer("Видалено!")
    except Exception:
        logger.exception("template_delete_confirm_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass