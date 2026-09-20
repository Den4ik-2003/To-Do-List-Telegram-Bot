from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_thread_ideas_actions(shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Нові ідеї", callback_data=f"threadsregen:{shop_id}")],
        [InlineKeyboardButton(text="◀️ До магазину", callback_data=f"shopopen:{shop_id}")],
    ])


def ikb_threads_ask(shop_id: str) -> InlineKeyboardMarkup:
    """Ранкове питання «Потрібні сьогодні Threads-пости?»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так", callback_data=f"threadsask:yes:{shop_id}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"threadsask:no:{shop_id}"),
    ]])


def ikb_threads_declined(shop_id: str) -> InlineKeyboardMarkup:
    """Після «Ні» лишаємо можливість передумати."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧵 Все ж згенерувати", callback_data=f"threadsask:yes:{shop_id}")],
    ])