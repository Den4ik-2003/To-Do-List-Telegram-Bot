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
STATUS_CANCELLED = "cancelled"

PROJECT_ACTIVE = "active"
PROJECT_DONE = "done"

GOAL_ACTIVE = "active"
GOAL_DONE = "done"

GOAL_TYPE_FINANCIAL = "financial"
GOAL_TYPE_GENERAL = "general"

TRANSACTION_INCOME = "income"
TRANSACTION_EXPENSE = "expense"

INCOME_CATEGORIES = {
    "job":        {"emoji": "💼", "name": "Робота"},
    "sales":      {"emoji": "🛒", "name": "Продажі"},
    "freelance":  {"emoji": "💻", "name": "Freelance"},
    "investment": {"emoji": "📈", "name": "Інвестиції"},
    "other":      {"emoji": "💰", "name": "Інше"},
}

EXPENSE_CATEGORIES = {
    "shopping":    {"emoji": "🛒", "name": "Покупки"},
    "food":        {"emoji": "🍔", "name": "Їжа"},
    "auto":        {"emoji": "🚗", "name": "Авто"},
    "home":        {"emoji": "🏠", "name": "Дім"},
    "work":        {"emoji": "💻", "name": "Робота"},
    "business":    {"emoji": "📦", "name": "Бізнес"},
    "advertising": {"emoji": "📢", "name": "Реклама"},
    "other":       {"emoji": "💰", "name": "Інше"},
}

DEFAULT_CURRENCY = "грн"
DEFAULT_MORNING_TIME = "09:00"

AI_CONTEXT_MAX_MESSAGES = 20
AI_CONVERSATION_SUMMARY_TRIGGER = 30

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

DB_ERROR_TEXT = "⚠️ Тимчасова проблема з базою даних. Спробуйте ще раз через кілька секунд."
AI_ERROR_TEXT = "⚠️ AI тимчасово недоступний. Спробуй пізніше — решта бота працює як завжди."
AI_LIMIT_REACHED_TEXT = "⚠️ Безкоштовні AI-запити на сьогодні закінчилися.\n\nСпробуйте завтра."