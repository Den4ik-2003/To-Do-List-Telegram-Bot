import logging
from datetime import datetime

from config.constants import INCOME_CATEGORIES, EXPENSE_CATEGORIES, TRANSACTION_INCOME, TRANSACTION_EXPENSE
from config.settings import AI_DAILY_LIMIT
from database import finances as finances_db
from database import ai_usage as ai_usage_db
from services import ai_service

logger = logging.getLogger("tasks_bot")


async def record_income(uid: int, amount: float, category: str, description: str = "", project_id: str | None = None) -> str:
    if category not in INCOME_CATEGORIES:
        category = "other"
    return await finances_db.add_transaction(uid, TRANSACTION_INCOME, amount, category, description, project_id=project_id)


async def record_expense(uid: int, amount: float, category: str, description: str = "", project_id: str | None = None) -> dict:
    if category not in EXPENSE_CATEGORIES:
        category = "other"
    txid = await finances_db.add_transaction(uid, TRANSACTION_EXPENSE, amount, category, description, project_id=project_id)

    budget_warning = None
    budget = await finances_db.get_budget_by_category(uid, category)
    if budget:
        await finances_db.increment_budget_spent(str(budget["_id"]), amount)
        new_spent = budget.get("spent", 0.0) + amount
        limit_amount = budget.get("limit_amount", 0.0)
        if limit_amount and new_spent > limit_amount:
            budget_warning = {
                "category": category,
                "spent": new_spent,
                "limit": limit_amount,
                "over_by": new_spent - limit_amount,
            }

    return {"transaction_id": txid, "budget_warning": budget_warning}


async def get_balance_summary(uid: int) -> dict:
    balance = await finances_db.get_balance(uid)
    now = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")
    month_end = now.strftime("%Y-%m-%dT23:59:59")
    month = await finances_db.get_period_summary(uid, month_start, month_end)
    return {
        "balance": balance,
        "month_income": month["income"],
        "month_expense": month["expense"],
        "month_net": month["net"],
    }


async def get_budgets_overview(uid: int) -> list:
    budgets = await finances_db.get_budgets(uid)
    overview = []
    for b in budgets:
        limit_amount = b.get("limit_amount", 0.0)
        spent = b.get("spent", 0.0)
        remaining = max(0.0, limit_amount - spent)
        percent = round(spent / limit_amount * 100) if limit_amount else 0
        overview.append({
            "id": str(b["_id"]),
            "category": b.get("category"),
            "limit": limit_amount,
            "spent": spent,
            "remaining": remaining,
            "percent": min(percent, 100),
        })
    return overview


async def parse_quick_transaction(uid: int, text: str) -> dict | None:
    if not ai_service.is_available():
        return None
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return None

    income_keys = ", ".join(INCOME_CATEGORIES.keys())
    expense_keys = ", ".join(EXPENSE_CATEGORIES.keys())

    prompt = f"""Розпізнай фінансову операцію з тексту користувача українською.

Текст: "{text}"

Категорії доходу: {income_keys}
Категорії витрати: {expense_keys}

Поверни ВИКЛЮЧНО валідний JSON без жодного тексту навколо, без markdown, у форматі:
{{
  "recognized": true,
  "type": "income|expense",
  "amount": 0,
  "category": "one_of_the_keys_above",
  "description": "короткий опис"
}}

Якщо в тексті немає фінансової операції — постав "recognized": false, а решту полів null."""

    data = await ai_service.generate_json(prompt)
    if not data or not data.get("recognized"):
        return None

    tx_type = data.get("type")
    if tx_type not in (TRANSACTION_INCOME, TRANSACTION_EXPENSE):
        return None
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    valid_categories = INCOME_CATEGORIES if tx_type == TRANSACTION_INCOME else EXPENSE_CATEGORIES
    category = data.get("category") if data.get("category") in valid_categories else "other"
    description = str(data.get("description") or "").strip()[:200]

    await ai_usage_db.increment_usage(uid)

    return {"type": tx_type, "amount": amount, "category": category, "description": description}