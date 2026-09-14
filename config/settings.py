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

# За замовчуванням Whisper тепер іде через OpenRouter (той самий ключ і base_url,
# що й основний AI), а не напряму через OpenAI. Якщо WHISPER_API_KEY не заданий
# окремо в env — падаємо назад на AI_API_KEY/AI_BASE_URL, щоб не треба було
# заводити окремий акаунт на platform.openai.com.
WHISPER_API_KEY = os.environ.get("WHISPER_API_KEY") or AI_API_KEY
WHISPER_BASE_URL = os.environ.get("WHISPER_BASE_URL") or AI_BASE_URL
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "openai/whisper-1")

OLX_CHECK_INTERVAL_MINUTES = int(os.environ.get("OLX_CHECK_INTERVAL_MINUTES", "30"))
RESALE_CHECK_INTERVAL_MINUTES = int(os.environ.get("RESALE_CHECK_INTERVAL_MINUTES", "180"))

# Моніторинг власних сайтів — перевіряємо часто, бо мета фічі — дізнатись
# про падіння сайту за секунди/хвилини, а не години.
SITE_CHECK_INTERVAL_MINUTES = int(os.environ.get("SITE_CHECK_INTERVAL_MINUTES", "2"))

# QA-скан (сторінки, форми, биті посилання, швидкість) — набагато важчий за
# простий uptime-пінг, тому запускаємо рідко, за замовчуванням раз на добу.
QA_CHECK_INTERVAL_HOURS = int(os.environ.get("QA_CHECK_INTERVAL_HOURS", "24"))
QA_MAX_PAGES = int(os.environ.get("QA_MAX_PAGES", "8"))

CURRENCY_UPDATE_TIME = os.environ.get("CURRENCY_UPDATE_TIME", "08:00")
WEATHER_MORNING_TIME = os.environ.get("WEATHER_MORNING_TIME", "07:30")

WORK_HOURS_TEXT = os.environ.get("WORK_HOURS_TEXT", "09:00–18:00")

# Пошук вакансій — за замовчуванням перевіряємо раз на годину.
JOB_CHECK_INTERVAL_MINUTES = int(os.environ.get("JOB_CHECK_INTERVAL_MINUTES", "60"))

# --- Творча студія: генерація/редагування зображень ---
IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "https://api.openai.com/v1")
IMAGE_GEN_MODEL = os.environ.get("IMAGE_GEN_MODEL", "gpt-image-1.5")

# скільки генерацій/редагувань на добу дозволено одному користувачу
CREATIVE_DAILY_LIMIT = int(os.environ.get("CREATIVE_DAILY_LIMIT", "15"))

# таймаут одного запиту до image API (генерація важча за текст)
IMAGE_REQUEST_TIMEOUT = int(os.environ.get("IMAGE_REQUEST_TIMEOUT", "120"))

# --- Моніторинг сторінок (контент-діф + AI-аналіз) ---
PAGE_CHECK_INTERVAL_MINUTES = int(os.environ.get("PAGE_CHECK_INTERVAL_MINUTES", "60"))
PAGE_HISTORY_DISPLAY_LIMIT = int(os.environ.get("PAGE_HISTORY_DISPLAY_LIMIT", "10"))

AI_WEEKLY_DIGEST_ENABLED = os.environ.get("AI_WEEKLY_DIGEST_ENABLED", "true").strip().lower() == "true"
AI_WEEKLY_DIGEST_DAY = os.environ.get("AI_WEEKLY_DIGEST_DAY", "mon")
AI_WEEKLY_DIGEST_HOUR = int(os.environ.get("AI_WEEKLY_DIGEST_HOUR", "9"))
AI_WEEKLY_DIGEST_MINUTE = int(os.environ.get("AI_WEEKLY_DIGEST_MINUTE", "30"))

PORT = int(os.environ.get("PORT", "8080"))

# --- 🔥 Знайти перепродаж: AI-моніторинг ---
RESALE_MIN_SCORE_THRESHOLD = int(os.environ.get("RESALE_MIN_SCORE_THRESHOLD", "70"))
RESALE_MAX_NOTIFY_PER_CYCLE = int(os.environ.get("RESALE_MAX_NOTIFY_PER_CYCLE", "3"))
RESALE_DEFAULT_CHECK_INTERVAL_MINUTES = int(os.environ.get("RESALE_DEFAULT_CHECK_INTERVAL_MINUTES", "180"))

def _parse_model_list(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip()]

# Через кому, напр.: AI_FALLBACK_MODELS=meta-llama/llama-3.1-8b-instruct:free,mistralai/mistral-7b-instruct:free
# Якщо не задано — fallback просто немає (поведінка як раніше, але без storm повторів).
AI_FALLBACK_MODELS = _parse_model_list(os.environ.get("AI_FALLBACK_MODELS", ""))

# Раз на добу "в обід" замість інтервалу — для OLX-трекерів та resale-моніторингу
OLX_CHECK_TIME = os.environ.get("OLX_CHECK_TIME", "13:00")
RESALE_CHECK_TIME = os.environ.get("RESALE_CHECK_TIME", "13:00")

# --- НОВЕ: 📅 Автоперенесення задач ---
# Максимум PENDING-задач на один день, понад який день вважається "зайнятим"
# і алгоритм автоперенесення шукає далі.
MAX_TASKS_PER_DAY = int(os.environ.get("MAX_TASKS_PER_DAY", "5"))
# Максимум сумарних estimated_minutes PENDING-задач на день — застосовується
# ДОДАТКОВО до MAX_TASKS_PER_DAY, тільки для задач, у яких estimated_minutes
# заданий (напр. створені через AI Планер). 480 хв = 8 год.
MAX_MINUTES_PER_DAY = int(os.environ.get("MAX_MINUTES_PER_DAY", "480"))

# --- НОВЕ: 🧹 AI-прибиральник (задачі, що довго не виконуються) ---
# Сам критерій "застаріла задача" — алгоритмічний (вік задачі), НЕ рішення
# AI-моделі, тому фіча працює навіть без AI_API_KEY (див.
# services/task_cleaner_service.py). AI, якщо доступний, лише додає
# опціональний коментар одним реченням.
AI_CLEANER_ENABLED = os.environ.get("AI_CLEANER_ENABLED", "true").strip().lower() == "true"
# Скільки днів без активності (прострочення терміну, або від created_at,
# якщо терміну немає) вважати "застарілою" задачею.
AI_CLEANER_STALE_DAYS = int(os.environ.get("AI_CLEANER_STALE_DAYS", "30"))
# День тижня й час розсилки дайджесту. mon/tue/wed/thu/fri/sat/sun.
AI_CLEANER_WEEKDAY = os.environ.get("AI_CLEANER_WEEKDAY", "mon")
AI_CLEANER_TIME = os.environ.get("AI_CLEANER_TIME", "10:00")

# --- AI Website Builder: розширення (редагування/фото-товар/замовлення) ---
NETLIFY_TOKEN = os.environ.get("NETLIFY_TOKEN", "")
GITHUB_TOKEN_ENCRYPTION_KEY = os.environ.get("GITHUB_TOKEN_ENCRYPTION_KEY", "")

ORDER_WEBHOOK_BASE_URL = os.environ.get("ORDER_WEBHOOK_BASE_URL", "")  # напр. https://mybot.up.railway.app
MAX_PRODUCT_IMAGE_BYTES = int(os.environ.get("MAX_PRODUCT_IMAGE_BYTES", str(1_200_000)))
PRODUCT_IMAGE_MAX_DIM = int(os.environ.get("PRODUCT_IMAGE_MAX_DIM", "1280"))
ORDERS_DISPLAY_LIMIT = int(os.environ.get("ORDERS_DISPLAY_LIMIT", "20"))