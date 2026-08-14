from datetime import datetime

from bson import ObjectId

from database.mongo import transactions_col, budgets_col, db_call

TRANSACTION_INCOME = "income"
TRANSACTION_EXPENSE = "expense"


async def add_transaction(
    uid: int,
    tx_type: str,
    amount: float,
    category: str,
    description: str = "",
    date: str | None = None,
    project_id: str | None = None,
) -> str:
    doc = {
        "uid": uid,
        "type": tx_type,
        "amount": amount,
        "category": category,
        "description": description,
        "date": date or datetime.now().isoformat(),
        "project_id": project_id,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(transactions_col.insert_one(doc))
    return str(result.inserted_id)


async def get_transaction(txid: str) -> dict | None:
    return await db_call(transactions_col.find_one({"_id": ObjectId(txid)}))


async def delete_transaction(txid: str):
    await db_call(transactions_col.delete_one({"_id": ObjectId(txid)}))


async def get_transactions(
    uid: int,
    start: str | None = None,
    end: str | None = None,
    tx_type: str | None = None,
    project_id: str | None = None,
    limit: int | None = None,
) -> list:
    query: dict = {"uid": uid}
    if tx_type:
        query["type"] = tx_type
    if project_id:
        query["project_id"] = project_id
    if start or end:
        date_query = {}
        if start:
            date_query["$gte"] = start
        if end:
            date_query["$lte"] = end
        query["date"] = date_query
    cursor = transactions_col.find(query).sort("date", -1)
    if limit:
        cursor = cursor.limit(limit)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_balance(uid: int) -> float:
    pipeline = [
        {"$match": {"uid": uid}},
        {"$group": {
            "_id": "$type",
            "total": {"$sum": "$amount"},
        }},
    ]
    cursor = transactions_col.aggregate(pipeline)
    rows = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    income = next((r["total"] for r in rows if r["_id"] == TRANSACTION_INCOME), 0.0)
    expense = next((r["total"] for r in rows if r["_id"] == TRANSACTION_EXPENSE), 0.0)
    return income - expense


async def get_period_summary(uid: int, start: str, end: str) -> dict:
    pipeline = [
        {"$match": {"uid": uid, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$type",
            "total": {"$sum": "$amount"},
        }},
    ]
    cursor = transactions_col.aggregate(pipeline)
    rows = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    income = next((r["total"] for r in rows if r["_id"] == TRANSACTION_INCOME), 0.0)
    expense = next((r["total"] for r in rows if r["_id"] == TRANSACTION_EXPENSE), 0.0)
    return {"income": income, "expense": expense, "net": income - expense}


async def get_category_breakdown(uid: int, tx_type: str, start: str, end: str) -> list:
    pipeline = [
        {"$match": {"uid": uid, "type": tx_type, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}},
    ]
    cursor = transactions_col.aggregate(pipeline)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def add_budget(uid: int, category: str, limit_amount: float, project_id: str | None = None) -> str:
    doc = {
        "uid": uid,
        "category": category,
        "limit_amount": limit_amount,
        "spent": 0.0,
        "project_id": project_id,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(budgets_col.insert_one(doc))
    return str(result.inserted_id)


async def get_budgets(uid: int) -> list:
    cursor = budgets_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []


async def get_budget_by_category(uid: int, category: str) -> dict | None:
    return await db_call(budgets_col.find_one({"uid": uid, "category": category}), raise_on_fail=False)


async def increment_budget_spent(bid: str, amount: float):
    await db_call(budgets_col.update_one({"_id": ObjectId(bid)}, {"$inc": {"spent": amount}}))


async def delete_budget(bid: str):
    await db_call(budgets_col.delete_one({"_id": ObjectId(bid)}))