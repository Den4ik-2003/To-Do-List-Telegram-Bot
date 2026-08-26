import logging
import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from database import resale as resale_db
from services import resale_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="resale")

_results_cache: dict[int, list[dict]] = {}

RESALE_TRIGGER_RE = re.compile(r"перепродаж", re.IGNORECASE)
BUDGET_RE = re.compile(r"до\s+(\d[\d\s]*)\s*грн", re.IGNORECASE)
COUNT_RE = re.compile(r"(\d+)\s*(можливост|товар|результат)", re.IGNORECASE)


class ResaleFlow(StatesGroup):
    waiting_budget = State()
    waiting_category = State()
    waiting_min_profit = State()
    waiting_min_margin = State()
    waiting_count = State()


def _ikb_sort() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Прибуток", callback_data="rs_sort:profit"),
         InlineKeyboardButton(text="📈 Маржа", callback_data="rs_sort:margin")],
        [InlineKeyboardButton(text="🔥 Найкращі", callback_data="rs_sort:perspective"),
         InlineKeyboardButton(text="🟢 Ризик", callback_data="rs_sort:risk")],
        [InlineKeyboardButton(text="⚡ Швидкість продажу", callback_data="rs_sort:speed")],
    ])


def _fmt_item(item: dict, idx: int) -> str:
    perspective = item.get("perspective")
    risk = item.get("risk", "невідомо")
    reasoning = item.get("reasoning", "")

    lines = [
        f"📦 *{item['title']}*" + (" 🔥" if idx == 0 else ""),
        "",
        f"💰 Купити: {item['purchase_price']:.0f} {item['currency']}",
        f"📊 Ринкова ціна: ~{item['market_price']:.0f} {item['currency']}",
        f"💵 Потенційний продаж: ~{item['resale_price']:.0f} {item['currency']}",
        f"💰 Потенційний прибуток: ~{item['profit']:.0f} {item['currency']}",
        f"📈 Маржа: ~{item['margin']:.0f}%",
        "",
    ]
    if perspective is not None:
        lines.append(f"🔥 Перспективність: {perspective}/10")
    lines.append(f"⚠️ Ризик: {risk}")
    if reasoning:
        lines.append(f"\n💡 {reasoning}")
    lines.append(f"\n🔗 {item['url']}")
    return "\n".join(lines)


def _ikb_item_actions(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Зберегti", callback_data=f"rs_save:{idx}"),
    ]])


async def _run_search(msg: Message, uid: int, budget, category, min_profit, min_margin, count):
    wait_msg = await msg.answer("🔎 Шукаю можливості для перепродажу на OLX, це займе трохи часу...")
    try:
        results = await resale_service.find_opportunities(budget, category, min_profit, min_margin, count)
    except Exception:
        logger.exception("resale search failed for uid=%s", uid)
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    if not results:
        return await wait_msg.edit_text(
            "📭 Нічого підходящого не знайшов за цими критеріями. Спробуй ширший бюджет "
            "або іншу категорію."
        )

    _results_cache[uid] = results
    await wait_msg.edit_text(f"✅ Знайдено {len(results)} можлив(ості/остей). Показую топ за маржею:")

    for i, item in enumerate(results):
        await msg.answer(_fmt_item(item, i), reply_markup=_ikb_item_actions(i))

    await msg.answer("Пересортувати результати?", reply_markup=_ikb_sort())


@router.message(F.text == "🔎 Знайти перепродаж")
async def resale_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.set_state(ResaleFlow.waiting_budget)
    await msg.answer(
        "💰 *Знайди мені перепродаж*\n\nВкажи бюджет у грн (макс. ціна купівлі), "
        "або напиши «немає»:",
        reply_markup=kb_cancel(),
    )


@router.message(ResaleFlow.waiting_budget)
async def resale_budget(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip().lower()
    budget = None
    if raw not in ("немає", "нема", "-"):
        try:
            budget = float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            return await msg.answer("⚠️ Введи число (напр. 5000) або «немає»:")
    await state.update_data(budget=budget)
    await state.set_state(ResaleFlow.waiting_category)
    await msg.answer("🏷 Категорія/тип товару (напр. `телефони`), або «немає» для широкого пошуку:")


@router.message(ResaleFlow.waiting_category)
async def resale_category(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    category = None if raw.lower() in ("немає", "нема", "-") else raw
    await state.update_data(category=category)
    await state.set_state(ResaleFlow.waiting_min_profit)
    await msg.answer("💵 Мінімальний бажаний прибуток у грн, або «немає»:")


@router.message(ResaleFlow.waiting_min_profit)
async def resale_min_profit(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip().lower()
    min_profit = None
    if raw not in ("немає", "нема", "-"):
        try:
            min_profit = float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(min_profit=min_profit)
    await state.set_state(ResaleFlow.waiting_min_margin)
    await msg.answer("📈 Мінімальна маржа у %, або «немає»:")


@router.message(ResaleFlow.waiting_min_margin)
async def resale_min_margin(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip().lower()
    min_margin = None
    if raw not in ("немає", "нема", "-"):
        try:
            min_margin = float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            return await msg.answer("⚠️ Введи число або «немає»:")
    await state.update_data(min_margin=min_margin)
    await state.set_state(ResaleFlow.waiting_count)
    await msg.answer("🔢 Скільки результатів показати? (напр. `10`)")


@router.message(ResaleFlow.waiting_count)
async def resale_count(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    try:
        count = int(msg.text.strip())
    except ValueError:
        return await msg.answer("⚠️ Введи ціле число, напр. `10`:")

    fd = await state.get_data()
    await state.clear()
    await _run_search(msg, msg.from_user.id, fd.get("budget"), fd.get("category"),
                       fd.get("min_profit"), fd.get("min_margin"), count)


@router.message(F.text.regexp(RESALE_TRIGGER_RE))
async def resale_natural_command(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    text = msg.text

    budget_match = BUDGET_RE.search(text)
    budget = float(budget_match.group(1).replace(" ", "")) if budget_match else None

    count_match = COUNT_RE.search(text)
    count = int(count_match.group(1)) if count_match else 10

    await _run_search(msg, msg.from_user.id, budget, None, None, None, count)


@router.callback_query(F.data.startswith("rs_sort:"))
async def resale_sort_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    results = _results_cache.get(uid)
    if not results:
        await cb.answer()
        return await cb.message.answer("⚠️ Результати застаріли, шукай наново через «🔎 Знайти перепродаж».")

    sort_key = cb.data.split(":", 1)[1]
    sorted_results = resale_service.sort_opportunities(results, sort_key)
    _results_cache[uid] = sorted_results

    await cb.answer("Пересортовано")
    for i, item in enumerate(sorted_results):
        await cb.message.answer(_fmt_item(item, i), reply_markup=_ikb_item_actions(i))


@router.callback_query(F.data.startswith("rs_save:"))
async def resale_save_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    idx = int(cb.data.split(":", 1)[1])
    results = _results_cache.get(uid) or []
    if idx >= len(results):
        return await cb.answer("Застаріло", show_alert=True)

    await resale_db.save_opportunity(uid, results[idx])
    await cb.answer("Збережено ⭐")


@router.message(F.text == "⭐ Збережені можливості")
async def resale_saved_list(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    saved = await resale_db.get_saved(msg.from_user.id)
    if not saved:
        return await msg.answer("📭 Немає збережених можливостей.", reply_markup=kb_main())

    for item in saved:
        text = (
            f"⭐ *{item['title']}*\n\n"
            f"💰 Купівля: {item['purchase_price']:.0f} {item['currency']}\n"
            f"📊 Ринкова ціна: {item['market_price']:.0f} {item['currency']}\n"
            f"💵 Прибуток: {item['profit']:.0f} {item['currency']} ({item['margin']:.0f}%)\n"
            f"🔗 {item['url']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"rs_del:{item['_id']}"),
        ]])
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("rs_del:"))
async def resale_delete_cb(cb: CallbackQuery):
    item_id = cb.data.split(":", 1)[1]
    ok = await resale_db.delete_saved(item_id, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text("🗑 Видалено зі збережених.")
        except Exception:
            pass