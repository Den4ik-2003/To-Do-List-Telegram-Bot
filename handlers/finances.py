import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery

from config.constants import (
    INCOME_CATEGORIES, EXPENSE_CATEGORIES, TRANSACTION_INCOME, TRANSACTION_EXPENSE,
    DB_ERROR_TEXT, DEFAULT_CURRENCY,
)
from database.mongo import DBUnavailable
from database import finances as finances_db
from database import goals as goals_db
from services import finance_service
from services import goal_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.finances import (
    ikb_finance_menu, kb_income_category, income_category_from_text,
    kb_expense_category, expense_category_from_text, ikb_quick_transaction_confirm,
    ikb_transactions_list, ikb_budgets_list, ikb_budget_actions,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="finances")

tx_list_cache: dict = {}
quick_tx_cache: dict = {}


class AddIncome(StatesGroup):
    amount = State()
    category = State()
    description = State()


class AddExpense(StatesGroup):
    amount = State()
    category = State()
    description = State()


class AddBudget(StatesGroup):
    category = State()
    limit = State()


class EditBudget(StatesGroup):
    limit = State()


def _parse_amount(text: str) -> float | None:
    raw = text.strip().replace(",", ".").replace(" ", "")
    raw = raw.replace("грн", "").replace("uah", "").replace("₴", "").strip()
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


# =========================================================
# ГОЛОВНИЙ ЕКРАН ФІНАНСІВ
# =========================================================

async def _fmt_finance_home(uid: int) -> str:
    summary = await finance_service.get_balance_summary(uid)
    net = summary["month_net"]
    sign = "+" if net >= 0 else ""
    return (
        "💰 *Фінанси*\n\n"
        f"💰 Поточний баланс\n*{summary['balance']:,.0f} {DEFAULT_CURRENCY}*\n".replace(",", " ") +
        "\n"
        f"📈 Доходи цього місяця\n*{summary['month_income']:,.0f} {DEFAULT_CURRENCY}*\n".replace(",", " ") +
        "\n"
        f"📉 Витрати цього місяця\n*{summary['month_expense']:,.0f} {DEFAULT_CURRENCY}*\n".replace(",", " ") +
        "\n"
        f"💵 Чистий результат\n*{sign}{net:,.0f} {DEFAULT_CURRENCY}*".replace(",", " ")
    )


@router.message(F.text == "💰 Фінанси")
async def finance_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    try:
        text = await _fmt_finance_home(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer(text, reply_markup=ikb_finance_menu())


@router.callback_query(F.data == "fin_close")
async def fin_close_cb(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await cb.answer()


@router.callback_query(F.data == "fin_menu_back")
async def fin_menu_back_cb(cb: CallbackQuery):
    try:
        text = await _fmt_finance_home(cb.from_user.id)
        await cb.message.edit_text(text, reply_markup=ikb_finance_menu())
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


# =========================================================
# ДОХІД
# =========================================================

@router.callback_query(F.data == "fin_add_income")
async def fin_add_income_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddIncome.amount)
    await cb.message.answer(f"➕ *Дохід*\n\nВведи суму в {DEFAULT_CURRENCY}:", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AddIncome.amount)
async def income_amount(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    amount = _parse_amount(msg.text)
    if amount is None:
        return await msg.answer("⚠️ Введи додатнє число, наприклад 10000:", reply_markup=kb_cancel())
    await state.update_data(amount=amount)
    await state.set_state(AddIncome.category)
    await msg.answer("Оберіть категорію:", reply_markup=kb_income_category())


@router.message(AddIncome.category)
async def income_category(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    category = income_category_from_text(msg.text)
    if not category:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_income_category())
    await state.update_data(category=category)
    await state.set_state(AddIncome.description)
    await msg.answer("Короткий опис (або «-», щоб пропустити):", reply_markup=kb_cancel())


@router.message(AddIncome.description)
async def income_description(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:200]
    fd = await state.get_data()
    try:
        await finance_service.record_income(msg.from_user.id, fd["amount"], fd["category"], desc)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    cat = INCOME_CATEGORIES.get(fd["category"], {})
    await msg.answer(
        f"✅ *Дохід додано!*\n\n+{fd['amount']:,.0f} {DEFAULT_CURRENCY}\n{cat.get('emoji','')} {cat.get('name','')}".replace(",", " "),
        reply_markup=kb_main(),
    )


# =========================================================
# ВИТРАТА
# =========================================================

@router.callback_query(F.data == "fin_add_expense")
async def fin_add_expense_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddExpense.amount)
    await cb.message.answer(f"➖ *Витрата*\n\nВведи суму в {DEFAULT_CURRENCY}:", reply_markup=kb_cancel())
    await cb.answer()


@router.message(AddExpense.amount)
async def expense_amount(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    amount = _parse_amount(msg.text)
    if amount is None:
        return await msg.answer("⚠️ Введи додатнє число, наприклад 350:", reply_markup=kb_cancel())
    await state.update_data(amount=amount)
    await state.set_state(AddExpense.category)
    await msg.answer("Оберіть категорію:", reply_markup=kb_expense_category())


@router.message(AddExpense.category)
async def expense_category(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    category = expense_category_from_text(msg.text)
    if not category:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_expense_category())
    await state.update_data(category=category)
    await state.set_state(AddExpense.description)
    await msg.answer("Короткий опис (або «-», щоб пропустити):", reply_markup=kb_cancel())


@router.message(AddExpense.description)
async def expense_description(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:200]
    fd = await state.get_data()
    try:
        result = await finance_service.record_expense(msg.from_user.id, fd["amount"], fd["category"], desc)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    cat = EXPENSE_CATEGORIES.get(fd["category"], {})
    text = (
        f"✅ *Витрату додано!*\n\n-{fd['amount']:,.0f} {DEFAULT_CURRENCY}\n{cat.get('emoji','')} {cat.get('name','')}"
    ).replace(",", " ")
    warning = result.get("budget_warning")
    if warning:
        text += (
            f"\n\n⚠️ Перевищено бюджет по категорії {cat.get('name','')}!\n"
            f"Витрачено {warning['spent']:,.0f} / ліміт {warning['limit']:,.0f} {DEFAULT_CURRENCY} "
            f"(перевищення {warning['over_by']:,.0f})".replace(",", " ")
        )
    await msg.answer(text, reply_markup=kb_main())


# =========================================================
# ІСТОРІЯ
# =========================================================

@router.callback_query(F.data == "fin_history")
async def fin_history_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        transactions = await finances_db.get_transactions(uid, limit=100)
        tx_list_cache[uid] = transactions
        if not transactions:
            await cb.message.edit_text("📋 *Історія*\n\n📭 Ще немає жодної операції.", reply_markup=ikb_finance_menu())
        else:
            await cb.message.edit_text(f"📋 *Історія* — {len(transactions)} операцій", reply_markup=ikb_transactions_list(transactions))
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


@router.callback_query(F.data.startswith("txpage:"))
async def tx_page_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        page = int(cb.data.split(":")[1])
        transactions = tx_list_cache.get(uid) or []
        await cb.message.edit_reply_markup(reply_markup=ikb_transactions_list(transactions, page))
        await cb.answer()
    except Exception:
        logger.exception("tx_page_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("txopen:"))
async def tx_open_cb(cb: CallbackQuery):
    try:
        txid = cb.data.split(":", 1)[1]
        tx = await finances_db.get_transaction(txid)
        if not tx:
            return await cb.answer("Не знайдено", show_alert=True)
        sign = "+" if tx.get("type") == TRANSACTION_INCOME else "-"
        cats = INCOME_CATEGORIES if tx.get("type") == TRANSACTION_INCOME else EXPENSE_CATEGORIES
        cat = cats.get(tx.get("category"), {})
        date_str = (tx.get("date") or "")[:16].replace("T", " ")
        text = (
            f"{'📈' if sign == '+' else '📉'} *{sign}{tx.get('amount',0):,.0f} {DEFAULT_CURRENCY}*\n\n"
            f"{cat.get('emoji','')} {cat.get('name','')}\n"
            f"📝 {tx.get('description') or '—'}\n"
            f"🕐 {date_str}"
        ).replace(",", " ")
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"txdel:{txid}")],
            [InlineKeyboardButton(text="◀️ До історії", callback_data="fin_history")],
        ])
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("tx_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("txdel:"))
async def tx_delete_cb(cb: CallbackQuery):
    try:
        txid = cb.data.split(":", 1)[1]
        await finances_db.delete_transaction(txid)
        await cb.message.edit_text("🗑 Операцію видалено.")
        await cb.answer("Видалено")
    except Exception:
        logger.exception("tx_delete_cb failed")
        await _safe_alert(cb)


# =========================================================
# СТАТИСТИКА ФІНАНСІВ
# =========================================================

@router.callback_query(F.data == "fin_stats")
async def fin_stats_cb(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        now = datetime.now()
        start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")
        end = now.strftime("%Y-%m-%dT23:59:59")
        income_breakdown = await finances_db.get_category_breakdown(uid, TRANSACTION_INCOME, start, end)
        expense_breakdown = await finances_db.get_category_breakdown(uid, TRANSACTION_EXPENSE, start, end)

        lines = ["📊 *Статистика фінансів (цей місяць)*", ""]
        lines.append("📈 *Доходи за категоріями:*")
        if income_breakdown:
            for row in income_breakdown:
                cat = INCOME_CATEGORIES.get(row["_id"], {})
                lines.append(f"{cat.get('emoji','')} {cat.get('name', row['_id'])} — {row['total']:,.0f} {DEFAULT_CURRENCY}".replace(",", " "))
        else:
            lines.append("(немає)")
        lines.append("")
        lines.append("📉 *Витрати за категоріями:*")
        if expense_breakdown:
            for row in expense_breakdown:
                cat = EXPENSE_CATEGORIES.get(row["_id"], {})
                lines.append(f"{cat.get('emoji','')} {cat.get('name', row['_id'])} — {row['total']:,.0f} {DEFAULT_CURRENCY}".replace(",", " "))
        else:
            lines.append("(немає)")

        await cb.message.edit_text("\n".join(lines), reply_markup=ikb_finance_menu())
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


# =========================================================
# ФІНАНСОВІ ЦІЛІ
# =========================================================

@router.callback_query(F.data == "fin_goals")
async def fin_goals_cb(cb: CallbackQuery):
    try:
        goals = await goals_db.get_active_financial_goals(cb.from_user.id)
        if not goals:
            await cb.message.edit_text(
                "🎯 *Фінансові цілі*\n\nЩе немає активних фінансових цілей.\n"
                "Створи їх у розділі «🎯 Мої цілі» → «➕ Додати ціль» → «💰 Фінансова ціль».",
                reply_markup=ikb_finance_menu(),
            )
            return await cb.answer()
        lines = ["🎯 *Фінансові цілі*", ""]
        for g in goals:
            percent = goal_service.progress_percent(g)
            bar = goal_service.progress_bar(percent)
            lines.append(f"*{g.get('title','')}*")
            lines.append(f"{bar} {percent}%")
            lines.append(f"{g.get('current_amount', 0)} / {g.get('target_amount')} {DEFAULT_CURRENCY}")
            lines.append("")
        await cb.message.edit_text("\n".join(lines).strip(), reply_markup=ikb_finance_menu())
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


# =========================================================
# БЮДЖЕТИ
# =========================================================

@router.callback_query(F.data == "fin_budgets")
async def fin_budgets_cb(cb: CallbackQuery):
    try:
        budgets = await finance_service.get_budgets_overview(cb.from_user.id)
        display = []
        for b in budgets:
            cat = EXPENSE_CATEGORIES.get(b["category"], {})
            display.append({"_id": b["id"], "title": f"{cat.get('emoji','')} {cat.get('name', b['category'])}"})
        await cb.message.edit_text("💳 *Бюджети*", reply_markup=ikb_budgets_list(display))
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)


@router.callback_query(F.data == "budget_add")
async def budget_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddBudget.category)
    await cb.message.answer("💳 Обери категорію витрат для бюджету:", reply_markup=kb_expense_category())
    await cb.answer()


@router.message(AddBudget.category)
async def budget_add_category(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    category = expense_category_from_text(msg.text)
    if not category:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_expense_category())
    existing = await finances_db.get_budget_by_category(msg.from_user.id, category)
    if existing:
        await state.clear()
        return await msg.answer("⚠️ Бюджет для цієї категорії вже існує. Відредагуй його зі списку бюджетів.", reply_markup=kb_main())
    await state.update_data(category=category)
    await state.set_state(AddBudget.limit)
    await msg.answer(f"Введи ліміт у {DEFAULT_CURRENCY}:", reply_markup=kb_cancel())


@router.message(AddBudget.limit)
async def budget_add_limit(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    amount = _parse_amount(msg.text)
    if amount is None:
        return await msg.answer("⚠️ Введи додатнє число:", reply_markup=kb_cancel())
    fd = await state.get_data()
    try:
        await finances_db.add_budget(msg.from_user.id, fd["category"], amount)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer("✅ Бюджет створено! AI буде враховувати його при плануванні.", reply_markup=kb_main())


@router.callback_query(F.data.startswith("budgetopen:"))
async def budget_open_cb(cb: CallbackQuery):
    try:
        bid = cb.data.split(":", 1)[1]
        budgets = await finances_db.get_budgets(cb.from_user.id)
        b = next((x for x in budgets if str(x["_id"]) == bid), None)
        if not b:
            return await cb.answer("Не знайдено", show_alert=True)
        cat = EXPENSE_CATEGORIES.get(b["category"], {})
        limit_amount = b.get("limit_amount", 0)
        spent = b.get("spent", 0)
        percent = round(spent / limit_amount * 100) if limit_amount else 0
        bar = "█" * min(10, round(percent / 10)) + "░" * max(0, 10 - round(percent / 10))
        text = (
            f"💳 *{cat.get('emoji','')} {cat.get('name', b['category'])}*\n\n"
            f"{bar} {percent}%\n"
            f"Витрачено: {spent:,.0f} / {limit_amount:,.0f} {DEFAULT_CURRENCY}".replace(",", " ")
        )
        await cb.message.edit_text(text, reply_markup=ikb_budget_actions(bid))
        await cb.answer()
    except Exception:
        logger.exception("budget_open_cb failed")
        await _safe_alert(cb)


@router.callback_query(F.data.startswith("budgetedit:"))
async def budget_edit_start(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 1)[1]
    await state.set_state(EditBudget.limit)
    await state.update_data(budget_id=bid)
    await cb.message.answer(f"Введи новий ліміт у {DEFAULT_CURRENCY}:", reply_markup=kb_cancel())
    await cb.answer()


@router.message(EditBudget.limit)
async def budget_edit_save(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    amount = _parse_amount(msg.text)
    if amount is None:
        return await msg.answer("⚠️ Введи додатнє число:", reply_markup=kb_cancel())
    fd = await state.get_data()
    from bson import ObjectId
    from database.mongo import budgets_col, db_call
    try:
        await db_call(budgets_col.update_one({"_id": ObjectId(fd["budget_id"])}, {"$set": {"limit_amount": amount}}))
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer("✅ Ліміт оновлено!", reply_markup=kb_main())


@router.callback_query(F.data.startswith("budgetdel:"))
async def budget_delete_cb(cb: CallbackQuery):
    try:
        bid = cb.data.split(":", 1)[1]
        await finances_db.delete_budget(bid)
        await cb.message.edit_text("🗑 Бюджет видалено.")
        await cb.answer("Видалено")
    except Exception:
        logger.exception("budget_delete_cb failed")
        await _safe_alert(cb)


# =========================================================
# ШВИДКЕ ДОДАВАННЯ ("Витратив 350 грн на бензин")
# =========================================================

@router.callback_query(F.data == "quicktx_confirm")
async def quicktx_confirm_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = quick_tx_cache.pop(uid, None)
    if not data:
        return await cb.answer("Сесія застаріла.", show_alert=True)
    try:
        if data["type"] == TRANSACTION_INCOME:
            await finance_service.record_income(uid, data["amount"], data["category"], data["description"])
        else:
            await finance_service.record_expense(uid, data["amount"], data["category"], data["description"])
    except DBUnavailable:
        return await cb.message.edit_text(DB_ERROR_TEXT)
    sign = "+" if data["type"] == TRANSACTION_INCOME else "-"
    await cb.message.edit_text(f"✅ Додано: {sign}{data['amount']:,.0f} {DEFAULT_CURRENCY}".replace(",", " "))
    await cb.answer()


@router.callback_query(F.data == "quicktx_cancel")
async def quicktx_cancel_cb(cb: CallbackQuery):
    quick_tx_cache.pop(cb.from_user.id, None)
    await cb.message.edit_text("❌ Не додано.")
    await cb.answer()


@router.message(StateFilter(None), F.text)
async def quick_add_catch_all(msg: Message, state: FSMContext):
    """
    Останній обробник у ланцюжку (реєструється останнім у todo.py).
    Спрацьовує, лише коли жоден інший хендлер/кнопка меню не підійшли
    і немає активного FSM — тобто це вільний текст типу
    "Витратив 350 грн на бензин".
    """
    try:
        authed = await require_auth(msg, state)
    except Exception:
        return
    if not authed:
        return

    uid = msg.from_user.id
    parsed = await finance_service.parse_quick_transaction(uid, msg.text or "")
    if not parsed:
        return

    quick_tx_cache[uid] = parsed
    cats = INCOME_CATEGORIES if parsed["type"] == TRANSACTION_INCOME else EXPENSE_CATEGORIES
    cat = cats.get(parsed["category"], {})
    sign = "+" if parsed["type"] == TRANSACTION_INCOME else "-"
    label = "дохід" if parsed["type"] == TRANSACTION_INCOME else "витрату"
    await msg.answer(
        f"💸 Знайдено {label}:\n\n"
        f"{sign}{parsed['amount']:,.0f} {DEFAULT_CURRENCY}\n"
        f"{cat.get('emoji','')} {cat.get('name','')}\n"
        f"{parsed['description'] or ''}\n\n"
        f"Додати?".replace(",", " "),
        reply_markup=ikb_quick_transaction_confirm(),
    )


async def _safe_alert(cb: CallbackQuery):
    try:
        await cb.answer(DB_ERROR_TEXT, show_alert=True)
    except TelegramAPIError:
        pass