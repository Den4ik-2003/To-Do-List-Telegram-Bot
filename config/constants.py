"""
Статичні константи, перенесені 1:1 з попереднього todo.py.
Значення НЕ змінені — тільки винесені в окремий модуль.
"""

LABELS = {
    "urgent":   {"emoji": "🔴", "name": "Терміново"},
    "medium":   {"emoji": "🟡", "name": "Середньо"},
    "low":      {"emoji": "🟢", "name": "Не поспішає"},
    "idea":     {"emoji": "🔵", "name": "Ідея"},
    "personal": {"emoji": "🟣", "name": "Особисте"},
}
LABEL_ORDER = {"urgent": 0, "medium": 1, "low": 2, "idea": 3, "personal": 4}
LABEL_XP = {"urgent": 25, "medium": 15, "low": 10, "idea": 10, "personal": 10}

CATEGORIES = {
    "work":    {"emoji": "💻", "name": "Робота"},
    "finance": {"emoji": "💰", "name": "Фінанси"},
    "home":    {"emoji": "🏠", "name": "Дім"},
    "sport":   {"emoji": "💪", "name": "Спорт"},
    "study":   {"emoji": "📚", "name": "Навчання"},
    "other":   {"emoji": "🗂", "name": "Інше"},
}

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

STATUS_PENDING = "pending"
STATUS_DONE = "done"

PROJECT_ACTIVE = "active"
PROJECT_DONE = "done"

DB_ERROR_TEXT = "⚠️ Тимчасова проблема з базою даних. Спробуйте ще раз через кілька секунд."
AI_ERROR_TEXT = "⚠️ AI-планувальник тимчасово недоступний. Спробуй пізніше — решта бота працює як завжди."

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]
# =========================================================
# ФІНАНСИ
# =========================================================
DEFAULT_CURRENCY = "грн"

TRANSACTION_INCOME = "income"
TRANSACTION_EXPENSE = "expense"

INCOME_CATEGORIES = {
    "salary":    {"emoji": "💼", "name": "Зарплата"},
    "freelance": {"emoji": "💻", "name": "Фріланс"},
    "project":   {"emoji": "📁", "name": "Проєкт"},
    "gift":      {"emoji": "🎁", "name": "Подарунок"},
    "other":     {"emoji": "🗂", "name": "Інше"},
}

EXPENSE_CATEGORIES = {
    "food":          {"emoji": "🍔", "name": "Їжа"},
    "transport":     {"emoji": "🚌", "name": "Транспорт"},
    "home":          {"emoji": "🏠", "name": "Дім"},
    "health":        {"emoji": "💊", "name": "Здоров'я"},
    "entertainment": {"emoji": "🎮", "name": "Розваги"},
    "shopping":      {"emoji": "🛍", "name": "Покупки"},
    "project":       {"emoji": "📁", "name": "Проєкт"},
    "other":         {"emoji": "🗂", "name": "Інше"},
}

# =========================================================
# ЦІЛІ
# =========================================================
GOAL_ACTIVE = "active"
GOAL_DONE = "done"

# =========================================================
# AI
# =========================================================
AI_LIMIT_TEXT = "🆓 Ліміт AI-запитів на сьогодні вичерпано. Спробуй завтра!"