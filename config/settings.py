

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

AI_API_KEY_BACKUP = os.environ.get("AI_API_KEY_BACKUP", "")
AI_BASE_URL_BACKUP = os.environ.get("AI_BASE_URL_BACKUP", AI_BASE_URL)
AI_MODEL_BACKUP = os.environ.get("AI_MODEL_BACKUP", AI_MODEL)

WHISPER_API_KEY = os.environ.get("WHISPER_API_KEY") or AI_API_KEY
WHISPER_BASE_URL = os.environ.get("WHISPER_BASE_URL") or AI_BASE_URL
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "openai/whisper-1")

OLX_CHECK_INTERVAL_MINUTES = int(os.environ.get("OLX_CHECK_INTERVAL_MINUTES", "30"))
RESALE_CHECK_INTERVAL_MINUTES = int(os.environ.get("RESALE_CHECK_INTERVAL_MINUTES", "180"))

SITE_CHECK_INTERVAL_MINUTES = int(os.environ.get("SITE_CHECK_INTERVAL_MINUTES", "2"))

QA_CHECK_INTERVAL_HOURS = int(os.environ.get("QA_CHECK_INTERVAL_HOURS", "24"))
QA_MAX_PAGES = int(os.environ.get("QA_MAX_PAGES", "8"))

CURRENCY_UPDATE_TIME = os.environ.get("CURRENCY_UPDATE_TIME", "08:00")
WEATHER_MORNING_TIME = os.environ.get("WEATHER_MORNING_TIME", "07:30")

THREADS_MORNING_TIME = os.environ.get("THREADS_MORNING_TIME", "08:30")

WORK_HOURS_TEXT = os.environ.get("WORK_HOURS_TEXT", "09:00–18:00")

EVENING_PLAN_TIME = os.environ.get("EVENING_PLAN_TIME", "22:00")
EVENING_PLAN_ENABLED = os.environ.get("EVENING_PLAN_ENABLED", "true").strip().lower() == "true"
EVENING_PLAN_HOUR_OPTIONS = [2, 4, 6, 8, 10, 12]

JOB_CHECK_INTERVAL_MINUTES = int(os.environ.get("JOB_CHECK_INTERVAL_MINUTES", "60"))

JOB_AUTOSEARCH_NOON_TIME = os.environ.get("JOB_AUTOSEARCH_NOON_TIME", "13:00")
JOB_AUTOSEARCH_EVENING_TIME = os.environ.get("JOB_AUTOSEARCH_EVENING_TIME", "19:00")

JOB_MIN_MATCH_PERCENT = int(os.environ.get("JOB_MIN_MATCH_PERCENT", "50"))

IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "https://api.openai.com/v1")
IMAGE_GEN_MODEL = os.environ.get("IMAGE_GEN_MODEL", "gpt-image-1.5")

CREATIVE_DAILY_LIMIT = int(os.environ.get("CREATIVE_DAILY_LIMIT", "15"))

IMAGE_REQUEST_TIMEOUT = int(os.environ.get("IMAGE_REQUEST_TIMEOUT", "120"))

PAGE_CHECK_INTERVAL_MINUTES = int(os.environ.get("PAGE_CHECK_INTERVAL_MINUTES", "60"))
PAGE_HISTORY_DISPLAY_LIMIT = int(os.environ.get("PAGE_HISTORY_DISPLAY_LIMIT", "10"))

AI_WEEKLY_DIGEST_ENABLED = os.environ.get("AI_WEEKLY_DIGEST_ENABLED", "true").strip().lower() == "true"
AI_WEEKLY_DIGEST_DAY = os.environ.get("AI_WEEKLY_DIGEST_DAY", "mon")
AI_WEEKLY_DIGEST_HOUR = int(os.environ.get("AI_WEEKLY_DIGEST_HOUR", "9"))
AI_WEEKLY_DIGEST_MINUTE = int(os.environ.get("AI_WEEKLY_DIGEST_MINUTE", "30"))

PORT = int(os.environ.get("PORT", "8080"))

RESALE_MIN_SCORE_THRESHOLD = int(os.environ.get("RESALE_MIN_SCORE_THRESHOLD", "70"))
RESALE_MAX_NOTIFY_PER_CYCLE = int(os.environ.get("RESALE_MAX_NOTIFY_PER_CYCLE", "3"))
RESALE_DEFAULT_CHECK_INTERVAL_MINUTES = int(os.environ.get("RESALE_DEFAULT_CHECK_INTERVAL_MINUTES", "180"))

def _parse_model_list(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip()]

AI_FALLBACK_MODELS = _parse_model_list(os.environ.get("AI_FALLBACK_MODELS", ""))

OLX_CHECK_TIME = os.environ.get("OLX_CHECK_TIME", "13:00")
RESALE_CHECK_TIME = os.environ.get("RESALE_CHECK_TIME", "13:00")

GITHUB_TOKEN_ENCRYPTION_KEY = os.environ.get("GITHUB_TOKEN_ENCRYPTION_KEY", "")

MAX_TASKS_PER_DAY = int(os.environ.get("MAX_TASKS_PER_DAY", "5"))
MAX_MINUTES_PER_DAY = int(os.environ.get("MAX_MINUTES_PER_DAY", "480"))

AI_CLEANER_ENABLED = os.environ.get("AI_CLEANER_ENABLED", "true").strip().lower() == "true"
AI_CLEANER_STALE_DAYS = int(os.environ.get("AI_CLEANER_STALE_DAYS", "30"))
AI_CLEANER_WEEKDAY = os.environ.get("AI_CLEANER_WEEKDAY", "mon")
AI_CLEANER_TIME = os.environ.get("AI_CLEANER_TIME", "10:00")

NETLIFY_TOKEN = os.environ.get("NETLIFY_TOKEN", "")

ORDER_WEBHOOK_BASE_URL = os.environ.get("ORDER_WEBHOOK_BASE_URL", "")
MAX_PRODUCT_IMAGE_BYTES = int(os.environ.get("MAX_PRODUCT_IMAGE_BYTES", str(1_200_000)))
PRODUCT_IMAGE_MAX_DIM = int(os.environ.get("PRODUCT_IMAGE_MAX_DIM", "1280"))
ORDERS_DISPLAY_LIMIT = int(os.environ.get("ORDERS_DISPLAY_LIMIT", "20"))

# --- НОВЕ: Cloudinary (unsigned upload) для фото товарів ---
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "dbhdmnxlx")
CLOUDINARY_UPLOAD_PRESET = os.environ.get("CLOUDINARY_UPLOAD_PRESET", "athelonImages")