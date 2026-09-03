"""
НОВИЙ ФАЙЛ: keyboards/kitchen.py

Inline-клавіатури фічі 🍳 Кухня. Стиль ідентичний keyboards/goals.py —
чисті функції-фабрики InlineKeyboardMarkup, без побічних ефектів.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_kitchen_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽️ Знайти рецепт", callback_data="kitchen_find")],
        [InlineKeyboardButton(text="🥕 Що приготувати з продуктів", callback_data="kitchen_from_products")],
        [InlineKeyboardButton(text="⚡ Щось швидке", callback_data="kitchen_quick")],
        [InlineKeyboardButton(text="💰 Бюджетна страва", callback_data="kitchen_budget")],
        [InlineKeyboardButton(text="🥩 Рецепт з конкретного продукту", callback_data="kitchen_single_product")],
        [InlineKeyboardButton(text="❤️ Обране", callback_data="kitchen_favorites")],
        [InlineKeyboardButton(text="📜 Історія рецептів", callback_data="kitchen_history")],
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="kitchen_shop_open")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_exit")],
    ])


def ikb_quick_time() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ 10 хв", callback_data="kitchen_quick_time:10"),
            InlineKeyboardButton(text="⚡ 20 хв", callback_data="kitchen_quick_time:20"),
        ],
        [
            InlineKeyboardButton(text="⚡ 30 хв", callback_data="kitchen_quick_time:30"),
            InlineKeyboardButton(text="⏰ До 1 години", callback_data="kitchen_quick_time:60"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_menu")],
    ])


def ikb_dish_list(dishes: list[dict], back_cb: str = "kitchen_menu") -> InlineKeyboardMarkup:
    rows = []
    for i, d in enumerate(dishes):
        emoji = d.get("emoji") or "🍽"
        title = (d.get("title") or "")[:60]
        rows.append([InlineKeyboardButton(text=f"{emoji} {title}", callback_data=f"kitchen_pick:{i}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_recipe_actions(is_favorite: bool = False) -> InlineKeyboardMarkup:
    fav_text = "💔 Прибрати з обраного" if is_favorite else "❤️ Додати в обране"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍🍳 Почати готувати", callback_data="kitchen_cook_start")],
        [
            InlineKeyboardButton(text="🔄 Інший рецепт", callback_data="kitchen_recipe_regen"),
            InlineKeyboardButton(text=fav_text, callback_data="kitchen_recipe_fav"),
        ],
        [
            InlineKeyboardButton(text="🛒 Список покупок", callback_data="kitchen_recipe_shopping"),
            InlineKeyboardButton(text="👥 Порції", callback_data="kitchen_recipe_servings"),
        ],
        [InlineKeyboardButton(text="🔄 Замінити інгредієнт", callback_data="kitchen_recipe_substitute")],
        [InlineKeyboardButton(text="⬅️ До меню Кухні", callback_data="kitchen_menu")],
    ])


def ikb_servings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="2", callback_data="kitchen_servings:2"),
            InlineKeyboardButton(text="4", callback_data="kitchen_servings:4"),
            InlineKeyboardButton(text="6", callback_data="kitchen_servings:6"),
            InlineKeyboardButton(text="8", callback_data="kitchen_servings:8"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад до рецепту", callback_data="kitchen_recipe_back")],
    ])


def ikb_cooking_step(step: int, total: int) -> InlineKeyboardMarkup:
    nav_row = []
    if step > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_cook_prev"))
    if step < total - 1:
        nav_row.append(InlineKeyboardButton(text="➡️ Готово", callback_data="kitchen_cook_next"))
    else:
        nav_row.append(InlineKeyboardButton(text="✅ Готово, я закінчив(ла)", callback_data="kitchen_cook_stop"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav_row,
        [
            InlineKeyboardButton(text="⏸ Пауза", callback_data="kitchen_cook_pause"),
            InlineKeyboardButton(text="❌ Завершити", callback_data="kitchen_cook_stop"),
        ],
    ])


def ikb_favorites_list(favorites: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for f in favorites:
        rows.append([
            InlineKeyboardButton(text=f"❤️ {(f.get('title') or '')[:45]}", callback_data=f"kitchen_fav_open:{f['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"kitchen_fav_del:{f['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_history_list(history: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for h in history:
        rows.append([InlineKeyboardButton(text=f"📜 {(h.get('title') or '')[:60]}", callback_data=f"kitchen_hist_open:{h['_id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_shopping_list(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        mark = "☑️" if it.get("checked") else "☐"
        rows.append([InlineKeyboardButton(text=f"{mark} {(it.get('name') or '')[:50]}", callback_data=f"kitchen_shop_toggle:{it['_id']}")])
    if items:
        rows.append([InlineKeyboardButton(text="🧹 Прибрати куплене", callback_data="kitchen_shop_clear")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_back_to_kitchen() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen_menu")]])