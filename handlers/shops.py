import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT
from database.mongo import DBUnavailable
from database import shops as shops_db
from services import channel_service
from keyboards.main_menu import kb_main
from keyboards.shops import (
    kb_shops_menu, kb_cancel_shop, ikb_shops_list, ikb_shop_menu,
    ikb_shop_delete_confirm, ikb_channel_menu,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="shops")

CANCEL_TEXT = "❌ Скасувати"

CHANNEL_ERROR_LABELS = {
    "empty": "⚠️ Введи посилання, юзернейм (@channel) або перешли повідомлення з каналу.",
    "not_found": "⚠️ Канал не знайдено. Перевір юзернейм і спробуй ще раз.",
    "not_channel": "⚠️ Це не канал. Прив'язати можна лише Telegram-канал.",
    "not_member": "⚠️ Бот ще не доданий у цей канал. Додай бота адміністратором каналу і спробуй ще раз.",
    "not_admin": "⚠️ Бот доданий у канал, але не є адміністратором. Признач боту права адміністратора.",
    "no_post_rights": "⚠️ Бот адміністратор, але без права публікації постів. Увімкни право «Публікація повідомлень».",
}


class ShopFlow(StatesGroup):
    add_title = State()
    rename_title = State()
    channel_ref = State()


def is_cancel(text: str | None) -> bool:
    return bool(text) and text.strip() == CANCEL_TEXT


@router.message(F.text == "🏪 Мої магазини")
async def shops_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        shops = await shops_db.get_shops(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer("🏪 *Мої магазини*", reply_markup=kb_shops_menu())
    if shops:
        await msg.answer("Обери магазин:", reply_markup=ikb_shops_list(shops))
    else:
        await msg.answer("📭 Ще немає жодного магазину. Натисни «➕ Додати магазин».")


@router.message(F.text == "➕ Додати магазин")
async def add_shop_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(ShopFlow.add_title)
    await msg.answer("📝 Введи назву магазину:", reply_markup=kb_cancel_shop())


@router.message(ShopFlow.add_title)
async def add_shop_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_shops_menu())
    title = (msg.text or "").strip()[:64]
    if not title:
        return await msg.answer("⚠️ Введи назву магазину:", reply_markup=kb_cancel_shop())
    try:
        await shops_db.add_shop(msg.from_user.id, title)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_shops_menu())
    await state.clear()
    await msg.answer(f"✅ Магазин «{title}» додано.", reply_markup=kb_shops_menu())


@router.callback_query(F.data == "shops_back")
async def shops_back_cb(cb: CallbackQuery):
    try:
        shops = await shops_db.get_shops(cb.from_user.id)
        if not shops:
            await cb.message.edit_text("📭 Ще немає жодного магазину.")
        else:
            await cb.message.edit_text("Обери магазин:", reply_markup=ikb_shops_list(shops))
        await cb.answer()
    except Exception:
        logger.exception("shops_back_cb failed")
        await _safe_alert(cb)


async def _render_shop_menu(shop_id: str):
    shop = await shops_db.get_shop(shop_id)
    if not shop:
        return None, None
    text = f"🏪 *{shop.get('title','')}*"
    if shop.get("channel_username"):
        text += f"\n📢 Канал: @{shop['channel_username']}"
    elif shop.get("channel_id"):
        text += "\n📢 Канал: підключено"
    return text, ikb_shop_menu(shop_id, has_channel=bool(shop.get("channel_id")))


@router.callback_query(F.data.startswith("shopopen:"))
async def shop_open_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        text, kb = await _render_shop_menu(shop_id)
        if text is None:
            return await cb.answer("Магазин не знайдено.", show_alert=True)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("shop_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("shoprename:"))
async def shop_rename_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(ShopFlow.rename_title)
        await state.update_data(rename_shop_id=shop_id)
        await cb.message.answer("✏️ Введи нову назву магазину:", reply_markup=kb_cancel_shop())
        await cb.answer()
    except Exception:
        logger.exception("shop_rename_cb failed")
        await _safe_alert(cb)


@router.message(ShopFlow.rename_title)
async def shop_rename_save(msg: Message, state: FSMContext):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_shops_menu())
    title = (msg.text or "").strip()[:64]
    if not title:
        return await msg.answer("⚠️ Введи назву:", reply_markup=kb_cancel_shop())
    fd = await state.get_data()
    shop_id = fd.get("rename_shop_id")
    await state.clear()
    try:
        await shops_db.rename_shop(shop_id, title)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_shops_menu())
    await msg.answer(f"✅ Назву змінено на «{title}».", reply_markup=kb_shops_menu())


@router.callback_query(F.data.startswith("shopdel:"))
async def shop_delete_ask_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await cb.message.edit_text(
            "🗑 Видалити магазин разом з усіма його шаблонами, прикладами, стікерами та артикулами?",
            reply_markup=ikb_shop_delete_confirm(shop_id),
        )
        await cb.answer()
    except Exception:
        logger.exception("shop_delete_ask_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("shopdelconfirm:"))
async def shop_delete_confirm_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await shops_db.delete_shop(shop_id)
        await cb.message.edit_text("🗑 Магазин видалено.")
        await cb.answer("Видалено!")
    except Exception:
        logger.exception("shop_delete_confirm_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("shopchannel:"))
async def shop_channel_menu_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        shop = await shops_db.get_shop(shop_id)
        if not shop:
            return await cb.answer("Магазин не знайдено.", show_alert=True)
        text = "📢 *Канал магазину*\n\n"
        if shop.get("channel_id"):
            text += f"Підключено: @{shop.get('channel_username') or shop.get('channel_title','')}"
        else:
            text += "Канал ще не підключено."
        await cb.message.edit_text(text, reply_markup=ikb_channel_menu(shop_id, has_channel=bool(shop.get("channel_id"))))
        await cb.answer()
    except Exception:
        logger.exception("shop_channel_menu_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("shopchbind:"))
async def shop_channel_bind_start_cb(cb: CallbackQuery, state: FSMContext):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await state.set_state(ShopFlow.channel_ref)
        await state.update_data(bind_shop_id=shop_id)
        await cb.message.answer(
            "🔗 Додай бота адміністратором каналу з правом «Публікація повідомлень», "
            "потім надішли сюди юзернейм каналу (@channel) або перешли будь-яке повідомлення з каналу:",
            reply_markup=kb_cancel_shop(),
        )
        await cb.answer()
    except Exception:
        logger.exception("shop_channel_bind_start_cb failed")
        await _safe_alert(cb)


@router.message(ShopFlow.channel_ref)
async def shop_channel_bind_save(msg: Message, state: FSMContext, bot):
    if is_cancel(msg.text):
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_shops_menu())

    fd = await state.get_data()
    shop_id = fd.get("bind_shop_id")

    ref = None
    if msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        ref = f"@{msg.forward_from_chat.username}" if msg.forward_from_chat.username else str(msg.forward_from_chat.id)
    elif msg.text:
        ref = msg.text.strip()

    if not ref:
        return await msg.answer("⚠️ Надішли юзернейм каналу (@channel) або перешли повідомлення з каналу:", reply_markup=kb_cancel_shop())

    chat, status = await channel_service.resolve_channel(bot, ref)
    if status != "ok":
        return await msg.answer(CHANNEL_ERROR_LABELS.get(status, "⚠️ Не вдалося перевірити канал."), reply_markup=kb_cancel_shop())

    await state.clear()
    try:
        await shops_db.set_channel(shop_id, chat.id, chat.title or "", chat.username)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_shops_menu())
    await msg.answer(f"✅ Канал «{chat.title}» прив'язано. Тепер можна публікувати пости.", reply_markup=kb_shops_menu())


@router.callback_query(F.data.startswith("shopchunbind:"))
async def shop_channel_unbind_cb(cb: CallbackQuery):
    try:
        shop_id = cb.data.split(":", 1)[1]
        await shops_db.unset_channel(shop_id)
        text, kb = await _render_shop_menu(shop_id)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer("Канал відв'язано")
    except Exception:
        logger.exception("shop_channel_unbind_cb failed")
        await _safe_alert(cb)


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass