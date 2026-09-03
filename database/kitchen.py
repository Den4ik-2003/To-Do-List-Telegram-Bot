"""
НОВИЙ ФАЙЛ: database/kitchen.py

Шар роботи з БД для фічі 🍳 Кухня. Раніше цього файлу не існувало взагалі —
звідси AttributeError: module 'database.kitchen' has no attribute
'add_history' (і, ймовірно, впав би так само на будь-якій іншій функції
з handlers/kitchen.py, якби добрався до неї раніше).

Реалізує РІВНО ті функції, які викликає handlers/kitchen.py:
  add_history, get_history, get_history_item,
  is_favorite, add_favorite, remove_favorite_by_title, remove_favorite,
  get_favorites, get_favorite,
  add_shopping_items, get_shopping_items, toggle_shopping_item,
  clear_checked_shopping_items,
  save_cooking_session, clear_cooking_session, get_cooking_session

Використовує вже наявні колекції з database/mongo.py:
  recipe_history_col, favorite_recipes_col, shopping_items_col, cooking_sessions_col
і той самий db_call()/DBUnavailable() підхід, що й у database/olx.py —
щоб помилки MongoDB прокидались нагору так само, як їх уже очікує
handlers/kitchen.py (try/except DBUnavailable → DB_ERROR_TEXT).

ВАЖЛИВО про формат id:
- kitchen_fav_open_cb / kitchen_fav_del_cb в handlers/kitchen.py роблять
  `rid = int(cb.data.split(":", 1)[1])` — тобто обране ідентифікується
  ЦІЛИМ числом (не Mongo ObjectId). Тому add_favorite генерує власний
  послідовний цілочисельний id через counters_col (той самий механізм,
  що вже використовується для задач/id-шних сутностей бота), а не
  повертає ObjectId.
- kitchen_hist_open_cb, навпаки, працює з рядковим _id
  (`hid = cb.data.split(":", 1)[1]`, а kitchen_history_cb явно робить
  `str(h["_id"])` перед побудовою клавіатури) — тобто історія
  ідентифікується стандартним Mongo ObjectId, переведеним у рядок.
- kitchen_shop_toggle_cb так само оперує рядковим Mongo ObjectId
  (`item_id = cb.data.split(":", 1)[1]`, і `str(i["_id"])` при формуванні
  клавіатури) — тобто список покупок теж на ObjectId.
"""

import logging
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

from database.mongo import (
    recipe_history_col,
    favorite_recipes_col,
    shopping_items_col,
    cooking_sessions_col,
    counters_col,
    db_call,
)

logger = logging.getLogger("tasks_bot")

# Скільки останніх рецептів зберігати в історії на користувача —
# щоб колекція не росла безмежно для активних користувачів.
MAX_HISTORY_PER_USER = 30


async def _next_seq(name: str) -> int:
    """
    Наскрізний генератор послідовних цілочисельних id (per-counter),
    той самий патерн, що вже використовується в решті бота для
    id-шних сутностей (напр. задач).
    """
    doc = await db_call(
        counters_col.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
    )
    return doc["seq"]


# ============================================================
# 📜 ІСТОРІЯ РЕЦЕПТІВ
# ============================================================

async def add_history(uid: int, recipe: dict) -> str:
    """Додає рецепт в історію користувача. Повертає рядковий Mongo _id запису."""
    doc = {
        "uid": uid,
        **recipe,
        "created_at": datetime.now().isoformat(),
    }
    result = await db_call(recipe_history_col.insert_one(doc))

    # Обрізаємо стару історію понад ліміт — без цього колекція росте
    # безмежно для користувачів, які часто генерують рецепти.
    try:
        cursor = recipe_history_col.find({"uid": uid}, {"_id": 1}).sort("created_at", -1).skip(MAX_HISTORY_PER_USER)
        old_ids = [d["_id"] for d in await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []]
        if old_ids:
            await db_call(recipe_history_col.delete_many({"_id": {"$in": old_ids}}), raise_on_fail=False)
    except Exception:
        logger.exception("kitchen: не вдалося обрізати стару історію рецептів для uid=%s", uid)

    return str(result.inserted_id)


async def get_history(uid: int, limit: int = 10) -> list[dict]:
    cursor = recipe_history_col.find({"uid": uid}).sort("created_at", -1).limit(limit)
    return await db_call(cursor.to_list(length=limit))


async def get_history_item(uid: int, hid: str) -> dict | None:
    try:
        oid = ObjectId(hid)
    except (InvalidId, TypeError):
        return None
    return await db_call(recipe_history_col.find_one({"_id": oid, "uid": uid}))


# ============================================================
# ❤️ ОБРАНЕ
# ============================================================

async def is_favorite(uid: int, title: str) -> bool:
    if not title:
        return False
    doc = await db_call(favorite_recipes_col.find_one({"uid": uid, "title": title}))
    return doc is not None


async def add_favorite(uid: int, recipe: dict) -> int:
    """Додає рецепт в обране. Повертає цілочисельний id (для kitchen_fav_open:/kitchen_fav_del:)."""
    new_id = await _next_seq("kitchen_favorites")
    doc = {
        "uid": uid,
        "id": new_id,
        **recipe,
        "created_at": datetime.now().isoformat(),
    }
    await db_call(favorite_recipes_col.insert_one(doc))
    return new_id


async def remove_favorite_by_title(uid: int, title: str) -> bool:
    result = await db_call(favorite_recipes_col.delete_one({"uid": uid, "title": title}))
    return result.deleted_count > 0


async def remove_favorite(uid: int, rid: int) -> bool:
    result = await db_call(favorite_recipes_col.delete_one({"uid": uid, "id": rid}))
    return result.deleted_count > 0


async def get_favorites(uid: int) -> list[dict]:
    cursor = favorite_recipes_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=200))


async def get_favorite(uid: int, rid: int) -> dict | None:
    return await db_call(favorite_recipes_col.find_one({"uid": uid, "id": rid}))


# ============================================================
# 🛒 СПИСОК ПОКУПОК
# ============================================================

async def add_shopping_items(uid: int, ingredients: list[str]) -> int:
    items = [i.strip() for i in (ingredients or []) if i and i.strip()]
    if not items:
        return 0
    now = datetime.now().isoformat()
    docs = [{"uid": uid, "text": text, "checked": False, "created_at": now} for text in items]
    result = await db_call(shopping_items_col.insert_many(docs))
    return len(result.inserted_ids)


async def get_shopping_items(uid: int) -> list[dict]:
    cursor = shopping_items_col.find({"uid": uid}).sort("created_at", 1)
    return await db_call(cursor.to_list(length=500))


async def toggle_shopping_item(uid: int, item_id: str) -> None:
    try:
        oid = ObjectId(item_id)
    except (InvalidId, TypeError):
        return
    current = await db_call(shopping_items_col.find_one({"_id": oid, "uid": uid}))
    if not current:
        return
    await db_call(
        shopping_items_col.update_one(
            {"_id": oid, "uid": uid},
            {"$set": {"checked": not current.get("checked", False)}},
        )
    )


async def clear_checked_shopping_items(uid: int) -> int:
    result = await db_call(shopping_items_col.delete_many({"uid": uid, "checked": True}))
    return result.deleted_count


# ============================================================
# 👨‍🍳 РЕЖИМ "ГОТУЄМО РАЗОМ" (одна активна сесія на користувача)
# ============================================================

async def save_cooking_session(uid: int, recipe: dict, step: int) -> None:
    await db_call(
        cooking_sessions_col.update_one(
            {"uid": uid},
            {"$set": {
                "recipe": recipe,
                "step": step,
                "updated_at": datetime.now().isoformat(),
            }},
            upsert=True,
        )
    )


async def get_cooking_session(uid: int) -> dict | None:
    return await db_call(cooking_sessions_col.find_one({"uid": uid}))


async def clear_cooking_session(uid: int) -> None:
    await db_call(cooking_sessions_col.delete_one({"uid": uid}), raise_on_fail=False)