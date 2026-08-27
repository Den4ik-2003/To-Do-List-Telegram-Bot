import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import site_watch as site_watch_db
from services import site_watch_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="site_watch")


class SiteWatch(StatesGroup):
    waiting_url = State()


def _ikb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати сайт", callback_data="sw_add")],
        [InlineKeyboardButton(text="📋 Мої сайти", callback_data="sw_list")],
    ])


@router.message(F.text == "🌐 Моніторинг сайтів")
async def site_watch_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "🌐 *Моніторинг власних сайтів*\n\n"
        "Додай сайт — і я перевірятиму, чи він доступний. "
        "Якщо впаде — миттєво напишу сюди.",
        reply_markup=_ikb_menu(),
    )


@router.callback_query(F.data == "sw_add")
async def site_watch_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SiteWatch.waiting_url)
    await cb.answer()
    await cb.message.answer(
        "🔗 Встав адресу сайту (напр. `example.com` або `https://example.com`):",
        reply_markup=kb_cancel(),
    )


@router.message(SiteWatch.waiting_url)
async def site_watch_add_url(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

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
        f"Перевірятиму регулярно і одразу повідомлю, якщо статус зміниться."
    )
    await msg.answer("Що далі?", reply_markup=_ikb_menu())


@router.callback_query(F.data == "sw_list")
async def site_watch_list(cb: CallbackQuery):
    watches = await site_watch_db.get_user_watches(cb.from_user.id)
    await cb.answer()

    if not watches:
        return await cb.message.answer("📭 У тебе ще немає сайтів на моніторингу.")

    lines = ["📋 *Твої сайти:*\n"]
    rows = []
    for w in watches:
        wid = str(w["_id"])
        status = w.get("last_status")
        icon = "🟢" if status is True else ("🔴" if status is False else "⏳")
        lines.append(f"{icon} {w.get('url', '')}")
        rows.append([InlineKeyboardButton(text=f"🗑 {w.get('url', '')[:35]}", callback_data=f"sw_del:{wid}")])

    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sw_del:"))
async def site_watch_delete(cb: CallbackQuery):
    wid = cb.data.split(":", 1)[1]
    ok = await site_watch_db.delete_watch(wid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Сайт видалено з моніторингу.")
        except Exception:
            pass