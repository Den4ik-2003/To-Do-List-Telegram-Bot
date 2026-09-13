from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_thread_ideas_actions(shop_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Нові ідеї", callback_data=f"threadsregen:{shop_id}")],
        [InlineKeyboardButton(text="◀️ До магазину", callback_data=f"shopopen:{shop_id}")],
    ])