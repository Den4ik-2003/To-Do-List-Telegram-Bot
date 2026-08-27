import logging
import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from database import business_ideas as business_db
from services import business_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="business")

BUSINESS_TRIGGER_RE = re.compile(r"зроби\s+бізнес", re.IGNORECASE)
IDEA_SPLIT_RE = re.compile(r"з\s+ідеї\s*:\s*", re.IGNORECASE)

# Кеш останнього списку ідей юзера (per uid) — щоб відкривати повний план
# по натисканню на конкретну ідею без зайвого походу в базу і без потреби
# тягати повний Mongo _id через callback_data (там ліміт 64 байти).
_ideas_cache: dict[int, list[dict]] = {}


class BusinessFlow(StatesGroup):
    waiting_idea = State()


def _fmt_plan_part1(plan: dict) -> str:
    econ = plan.get("economics", {})
    return (
        f"💡 *ІДЕЯ*\n{plan.get('idea', '')}\n\n"
        f"🎯 *ЦІЛЬОВИЙ КЛІЄНТ*\n{plan.get('target_client', '')}\n\n"
        f"📦 *ПРОДУКТ/ПОСЛУГА*\n{plan.get('product', '')}\n\n"
        f"💰 *МОДЕЛЬ МОНЕТИЗАЦІЇ*\n{plan.get('monetization', '')}\n\n"
        f"💵 *СТАРТОВИЙ БЮДЖЕТ*\n{plan.get('starting_budget_uah', '?')} грн\n\n"
        f"📊 *ЕКОНОМІКА*\n"
        f"Собівартість: {econ.get('cost_uah', '?')} грн\n"
        f"Ціна продажу: {econ.get('price_uah', '?')} грн\n"
        f"Прибуток: {econ.get('profit_uah', '?')} грн\n"
        f"Маржа: {econ.get('margin_percent', '?')}%"
    )


def _fmt_plan_part2(plan: dict) -> str:
    market = plan.get("market", {})
    channels = ", ".join(plan.get("sales_channels", []))
    risks = "\n".join(f"• {r}" for r in plan.get("risks", []))
    return (
        f"📢 *КАНАЛИ ПРОДАЖУ*\n{channels}\n\n"
        f"🥊 *КОНКУРЕНЦІЯ*\n{plan.get('competition', '')}\n"
        f"_(орієнтовна оцінка AI, не факт-чек живих даних)_\n\n"
        f"📈 *РИНОК*\n"
        f"Попит: {market.get('demand', '')}\n"
        f"Конкуренція: {market.get('competition_level', '')}\n"
        f"Сезонність: {market.get('seasonality', '')}\n"
        f"Маржа: {market.get('potential_margin', '')}\n\n"
        f"⚠️ *РИЗИКИ*\n{risks}"
    )


def _fmt_plan_part3(plan: dict) -> str:
    scores = plan.get("scores", {})
    days = plan.get("plan_7_days", [])
    days_text = "\n".join(f"День {d.get('day')}: {d.get('actions')}" for d in days)
    return (
        f"🚀 *MVP*\n{plan.get('mvp', '')}\n\n"
        f"📅 *ПЛАН НА 7 ДНІВ*\n{days_text}\n\n"
        f"💰 *ПЕРШІ ГРОШІ*\n{plan.get('first_money', '')}\n\n"
        f"📊 *ОЦІНКА*\n"
        f"Потенціал: {scores.get('potential', '?')}/10\n"
        f"Складність: {scores.get('difficulty', '?')}/10\n"
        f"Стартовий бюджет: {scores.get('budget_uah', '?')} грн\n"
        f"Ризик: {scores.get('risk', '?')}/10\n"
        f"Конкуренція: {scores.get('competition', '?')}/10\n"
        f"Швидкість перших грошей: {scores.get('speed_to_first_money', '?')}/10"
    )


def _ikb_ideas_list(ideas: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, idea in enumerate(ideas[:20]):
        title = (idea.get("idea_text", "") or "")[:40] or "(без назви)"
        rows.append([InlineKeyboardButton(text=f"💡 {title}", callback_data=f"bizopen:{i}")])
    rows.append([InlineKeyboardButton(text="◀️ Головне меню", callback_data="biz_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ikb_idea_detail_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ До списку ідей", callback_data="biz_list_back"),
    ]])


async def _process_idea(msg: Message, idea_text: str):
    wait_msg = await msg.answer("💡 Аналізую ідею, роблю бізнес-план...")
    plan = await business_service.generate_business_plan(idea_text)
    if not plan:
        # ВИПРАВЛЕНО: раніше тут просто edit_text(AI_ERROR_TEXT) і все —
        # edit_text НЕ може замінити нижню reply-клавіатуру. Стан вже
        # очищено (в business_idea_received), тож стара кнопка
        # "❌ Скасувати" лишалась висіти на екрані, ні на що не реагуючи.
        await wait_msg.edit_text(AI_ERROR_TEXT)
        return await msg.answer("🏠 Головне меню:", reply_markup=kb_main())

    await business_db.save_idea(msg.from_user.id, idea_text, plan)

    await wait_msg.edit_text(_fmt_plan_part1(plan))
    await msg.answer(_fmt_plan_part2(plan))
    await msg.answer(_fmt_plan_part3(plan))
    await msg.answer("🏠 Головне меню:", reply_markup=kb_main())


@router.message(F.text == "💡 Зробити з ідеї бізнес")
async def business_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(BusinessFlow.waiting_idea)
    await msg.answer(
        "💡 *Зроби з цього бізнес*\n\nНапиши сиру ідею, напр. «хочу продавати новорічні товари»:",
        reply_markup=kb_cancel(),
    )


@router.message(BusinessFlow.waiting_idea)
async def business_idea_received(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    idea_text = msg.text.strip()
    await state.clear()
    await _process_idea(msg, idea_text)


@router.message(F.text.regexp(BUSINESS_TRIGGER_RE))
async def business_natural_command(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    text = msg.text
    parts = IDEA_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await state.set_state(BusinessFlow.waiting_idea)
        return await msg.answer("Опиши ідею детальніше:", reply_markup=kb_cancel())
    await _process_idea(msg, parts[1].strip())


@router.message(F.text == "📊 Мої бізнес-ідеї")
async def business_list(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    ideas = await business_db.get_user_ideas(msg.from_user.id)
    if not ideas:
        return await msg.answer("📭 Ще немає збережених бізнес-ідей.", reply_markup=kb_main())

    _ideas_cache[msg.from_user.id] = ideas
    await msg.answer(
        f"📊 *Твої бізнес-ідеї* — {len(ideas[:20])} шт.\n\nНатисни на ідею, щоб переглянути повний план:",
        reply_markup=_ikb_ideas_list(ideas),
    )


@router.callback_query(F.data.startswith("bizopen:"))
async def business_idea_open_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    ideas = _ideas_cache.get(uid)
    if not ideas:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Список застарів, відкрий «📊 Мої бізнес-ідеї» ще раз.")

    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    if idx < 0 or idx >= len(ideas):
        return await cb.answer()

    idea = ideas[idx]
    plan = idea.get("plan")
    await cb.answer()

    if not plan:
        return await cb.message.edit_text(
            f"💡 {idea.get('idea_text', '')}\n\n⚠️ Деталей плану для цієї ідеї не збережено.",
            reply_markup=_ikb_idea_detail_back(),
        )

    await cb.message.edit_text(_fmt_plan_part1(plan))
    await cb.message.answer(_fmt_plan_part2(plan))
    await cb.message.answer(_fmt_plan_part3(plan), reply_markup=_ikb_idea_detail_back())


@router.callback_query(F.data == "biz_list_back")
async def business_list_back_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    ideas = _ideas_cache.get(uid)
    if not ideas:
        ideas = await business_db.get_user_ideas(uid)
        _ideas_cache[uid] = ideas
    if not ideas:
        return await cb.message.edit_text("📭 Ще немає збережених бізнес-ідей.")
    await cb.message.edit_text(
        f"📊 *Твої бізнес-ідеї* — {len(ideas[:20])} шт.\n\nНатисни на ідею, щоб переглянути повний план:",
        reply_markup=_ikb_ideas_list(ideas),
    )


@router.callback_query(F.data == "biz_close")
async def business_close_cb(cb: CallbackQuery):
    _ideas_cache.pop(cb.from_user.id, None)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())