"""
keyboards/task_cleaner.py

Клавіатура для одиничного тижневого повідомлення "🧹 AI-прибиральник".
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_cleaner_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Видалити всі", callback_data="cleaner_delete_all")],
        [InlineKeyboardButton(text="📋 Обрати самому", callback_data="cleaner_pick")],
        [InlineKeyboardButton(text="🔕 Залишити всі", callback_data="cleaner_dismiss_all")],
    ])