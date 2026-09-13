"""
НОВИЙ ФАЙЛ: keyboards/ai_developer.py

Клавіатури фічі "👨‍💻 AI Developer". Стиль — 1:1 як у keyboards/github.py
(callback_data-патерни з двокрапкою/pid), просто новий namespace "aidev_".
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def ikb_ai_developer_menu(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI аналіз проєкту", callback_data=f"aidev_analyze:{pid}")],
        [InlineKeyboardButton(text="🐛 Знайти баги", callback_data=f"aidev_bugs:{pid}")],
        [InlineKeyboardButton(text="🔒 Security Check", callback_data=f"aidev_security:{pid}")],
        [InlineKeyboardButton(text="⚡ Оптимізувати", callback_data=f"aidev_optimize:{pid}")],
        [InlineKeyboardButton(text="✏️ Змінити код", callback_data=f"aidev_edit:{pid}")],
        [InlineKeyboardButton(text="💬 Запитати про код", callback_data=f"aidev_ask:{pid}")],
        [InlineKeyboardButton(text="📜 Історія AI-змін", callback_data=f"aidev_history:{pid}")],
        [InlineKeyboardButton(text="↩️ Скасувати останню зміну", callback_data=f"aidev_undo:{pid}")],
        [InlineKeyboardButton(text="🔄 Оновити з GitHub", callback_data=f"aidev_refresh:{pid}")],
        [InlineKeyboardButton(text="◀️ До проєкту", callback_data=f"ghproj:{pid}")],
    ])


def ikb_back_to_ai_menu(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ До AI Developer", callback_data=f"aidev_open:{pid}")],
    ])


def ikb_change_confirm(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Застосувати зміни", callback_data=f"aidev_apply:{pid}")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"aidev_cancel_plan:{pid}")],
    ])


def ikb_undo_confirm(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, скасувати зміну", callback_data=f"aidev_undo_confirm:{pid}")],
        [InlineKeyboardButton(text="❌ Ні", callback_data=f"aidev_open:{pid}")],
    ])