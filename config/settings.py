import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
MONGO_URI = os.environ["MONGO_URI"]

REMINDER_BEFORE_MINUTES = int(os.environ.get("REMINDER_BEFORE_MINUTES", "10"))
DAILY_REPORT_TIME = os.environ.get("DAILY_REPORT_TIME", "21:00")

AI_API_KEY = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.environ.get("AI_MODEL", "google/gemini-2.0-flash-exp:free")
AI_DAILY_PLAN_TIME = os.environ.get("AI_DAILY_PLAN_TIME", "09:00")
AI_DAILY_PLAN_ENABLED = os.environ.get("AI_DAILY_PLAN_ENABLED", "true").strip().lower() == "true"

AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "10"))

CURRENCY_UPDATE_TIME = os.environ.get("CURRENCY_UPDATE_TIME", "08:00")
WEATHER_MORNING_TIME = os.environ.get("WEATHER_MORNING_TIME", "07:30")

WORK_HOURS_TEXT = os.environ.get("WORK_HOURS_TEXT", "09:00–18:00")

PORT = int(os.environ.get("PORT", "8080"))