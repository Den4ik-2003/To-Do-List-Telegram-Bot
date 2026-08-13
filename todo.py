import asyncio
import csv
import io
import json
import logging
import os
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BufferedInputFile,
)
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError
from bson import ObjectId
from openai import AsyncOpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tasks_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
MONGO_URI = os.environ["MONGO_URI"]

REMINDER_BEFORE_MINUTES = int(os.environ.get("REMINDER_BEFORE_MINUTES", "10"))
DAILY_REPORT_TIME = os.environ.get("DAILY_REPORT_TIME", "21:00")

# ---- OpenAI / AI Планер ----
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
AI_DAILY_PLAN_TIME = os.environ.get("AI_DAILY_PLAN_TIME", "09:00")
AI_DAILY_PLAN_ENABLED = os.environ.get("AI_DAILY_PLAN_ENABLED", "true").strip().lower() == "true"

# Робочий графік користувача (враховується AI при плануванні часу задач)
WORK_HOURS_TEXT = "09:00–18:00"

openai_client: AsyncOpenAI | None = None
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
else:
    logger.warning("OPENAI_API_KEY не задано — AI Планер вимкнено (звичайний функціонал бота працює як завжди)")

mongo_client: AsyncIOMotorClient | None = None
db = None
tasks_col = None
users_col = None
auth_col = None
counters_col = None
goals_col = None

authorized_uids: set[int] = set()

def init_mongo():
    global mongo_client, db, tasks_col, users_col, auth_col, counters_col, goals_col
    mongo_client = AsyncIOMotorClient(
        MONGO_URI,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=15000,
        maxPoolSize=20,
        retryWrites=True,
    )
    db = mongo_client["tasks_bot"]
    tasks_col = db["tasks"]
    users_col = db["users"]
    auth_col = db["auth"]
    counters_col = db["counters"]
    goals_col = db["goals"]

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

DB_ERROR_TEXT = "⚠️ Тимчасова проблема з базою даних. Спробуйте ще раз через кілька секунд."
AI_ERROR_TEXT = "⚠️ AI-планувальник тимчасово недоступний. Спробуй пізніше — решта бота працює як завжди."

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

class DBUnavailable(Exception):
    pass

async def db_call(coro, default=None, retries=2, raise_on_fail=True):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await coro
        except PyMongoError as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue
    logger.exception("MongoDB error: %s", last_exc)
    if raise_on_fail:
        raise DBUnavailable(str(last_exc)) from last_exc
    return default

async def is_authorized(uid: int) -> bool:
    if uid in authorized_uids:
        return True
    doc = await db_call(auth_col.find_one({"uid": uid}))
    if doc is not None:
        authorized_uids.add(uid)
        return True
    return False

async def authorize(uid: int):
    authorized_uids.add(uid)
    await db_call(auth_col.update_one({"uid": uid}, {"$set": {"uid": uid}}, upsert=True))

async def load_authorized_uids():
    cursor = auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    for d in docs:
        if "uid" in d:
            authorized_uids.add(d["uid"])
    logger.info("Loaded %d authorized users into cache", len(authorized_uids))

async def get_all_uids() -> list:
    if authorized_uids:
        return list(authorized_uids)
    cursor = auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
    return [d["uid"] for d in docs if "uid" in d]

async def get_user_state(uid: int) -> dict:
    doc = await db_call(users_col.find_one({"uid": uid}))
    if not doc:
        return {
            "uid": uid, "streak": 0, "last_streak_date": "",
            "archive_prompt_month": "", "xp": 0,
            "total_completed": 0, "total_missed": 0, "total_postponed": 0,
            "last_ai_plan_date": "", "ai_morning_enabled": True,
        }
    doc.setdefault("xp", 0)
    doc.setdefault("total_completed", 0)
    doc.setdefault("total_missed", 0)
    doc.setdefault("total_postponed", 0)
    doc.setdefault("last_ai_plan_date", "")
    doc.setdefault("ai_morning_enabled", True)
    return doc

async def save_user_state(uid: int, fields: dict):
    await db_call(users_col.update_one({"uid": uid}, {"$set": fields}, upsert=True))

async def next_task_id() -> int:
    doc = await db_call(
        counters_col.find_one_and_update(
            {"_id": "task_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
    )
    return doc["seq"]

async def add_task(task: dict):
    await db_call(tasks_col.insert_one(task))

async def get_task(tid: int) -> dict | None:
    return await db_call(tasks_col.find_one({"id": tid}, {"_id": 0}))

async def update_task(tid: int, fields: dict):
    await db_call(tasks_col.update_one({"id": tid}, {"$set": fields}))

async def delete_task(tid: int):
    await db_call(tasks_col.delete_one({"id": tid}))

async def get_user_tasks(uid: int, statuses: list | None = None) -> list:
    query = {"uid": uid}
    if statuses:
        query["status"] = {"$in": statuses}
    cursor = tasks_col.find(query, {"_id": 0})
    tasks = await db_call(cursor.to_list(length=None), default=[]) or []
    return tasks

def sort_tasks(tasks: list) -> list:
    def key(t):
        return (t.get("due", ""), LABEL_ORDER.get(t.get("label", "idea"), 9))
    return sorted(tasks, key=key)

def sort_tasks_by_label_then_due(tasks: list) -> list:
    def key(t):
        return (LABEL_ORDER.get(t.get("label", "idea"), 9), t.get("due", ""))
    return sorted(tasks, key=key)

def parse_due(due_str: str) -> datetime | None:
    try:
        return datetime.strptime(due_str, "%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return None

def fmt_due(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")

def is_today(due_str: str) -> bool:
    dt = parse_due(due_str)
    return bool(dt and dt.date() == datetime.now().date())

def is_missed(t: dict) -> bool:
    if t.get("status") != STATUS_PENDING:
        return False
    due = parse_due(t.get("due", ""))
    return bool(due and due <= datetime.now())

def time_remaining_str(due_dt: datetime) -> str:
    now = datetime.now()
    secs = (due_dt - now).total_seconds()
    if secs <= 0:
        return "⚫ Прострочено"
    if secs <= 1800:
        m = max(1, int(secs // 60))
        return f"🔴 Через {m} хв"
    if due_dt.date() == now.date():
        total_min = int(secs // 60)
        h, m = divmod(total_min, 60)
        parts = []
        if h:
            parts.append(f"{h} год")
        if m:
            parts.append(f"{m} хв")
        return "🟢 Через " + " ".join(parts) if parts else "🟢 Скоро"
    if due_dt.date() == (now + timedelta(days=1)).date():
        return "🟡 Завтра"
    days = (due_dt.date() - now.date()).days
    return f"⚫ Через {days} дн."

def level_progress(xp: int):
    level = 1
    total = 0
    threshold = 100
    while xp >= total + threshold:
        total += threshold
        level += 1
        threshold = 100 + level * 150
    into_level = xp - total
    return level, into_level, threshold

def fmt_task(t: dict, short: bool = False) -> str:
    label = LABELS.get(t.get("label", "idea"), {"emoji": "", "name": "—"})
    cat = CATEGORIES.get(t.get("category", "other"), {"emoji": "", "name": "—"})
    status = t.get("status", STATUS_PENDING)
    if status == STATUS_DONE:
        status_icon = "✅"
    elif is_missed(t):
        status_icon = "⚠️"
    else:
        status_icon = "⏳"
    pin_str = "📌 " if t.get("pinned") else ""

    due_dt = parse_due(t.get("due", ""))
    remain = ""
    if due_dt and status != STATUS_DONE:
        remain = "  " + time_remaining_str(due_dt)

    src = " 🤖" if t.get("source") == "ai" else ""

    lines = [
        f"{pin_str}*№{t['id']}* {label['emoji']} {status_icon}{src}",
        f"📝 {t.get('text', '')}",
        f"🕐 {t.get('due', '')}{remain}",
        f"🏷 {cat['emoji']} {cat['name']}   {label['emoji']} {label['name']}",
    ]
    subtasks = t.get("subtasks") or []
    if subtasks:
        done_n = sum(1 for s in subtasks if s.get("done"))
        lines.append(f"📝 Підзадачі: *{done_n}/{len(subtasks)}*")
    if not short:
        if t.get("postponed_count"):
            lines.append(f"🔁 Перенесено разів: *{t['postponed_count']}*")
        if status == STATUS_DONE and t.get("completed_at"):
            lines.append(f"✅ Виконано: {t['completed_at'][:16].replace('T', ' ')}")
    return "\n".join(lines)

def build_task_list_text(tasks: list, title: str) -> str:
    if not tasks:
        return f"{title}\n\n📭 Немає завдань."
    lines = [title, ""]
    for t in tasks:
        label = LABELS.get(t.get("label", "idea"), {"emoji": ""})
        status_icon = "✅" if t.get("status") == STATUS_DONE else ("⚠️" if is_missed(t) else "⏳")
        pin_str = "📌" if t.get("pinned") else ""
        lines.append(f"{status_icon} {pin_str}{label['emoji']} №{t['id']} — {t.get('due','')[-5:]} {t.get('text','')[:35]}")
    return "\n".join(lines)

async def compute_daily_stats(uid: int) -> dict:
    today_str = datetime.now().strftime("%d.%m.%Y")
    tasks = await get_user_tasks(uid)
    done_today, missed_today, postponed_today = [], [], 0
    longest = None
    for t in tasks:
        due = t.get("due", "")
        if not due.startswith(today_str):
            continue
        if t.get("status") == STATUS_DONE:
            done_today.append(t)
            created = t.get("created_at")
            completed = t.get("completed_at")
            if created and completed:
                try:
                    delta = (datetime.fromisoformat(completed) - datetime.fromisoformat(created)).total_seconds()
                    if longest is None or delta > longest[0]:
                        longest = (delta, t.get("text", ""))
                except ValueError:
                    pass
        elif is_missed(t):
            missed_today.append(t)
        if t.get("postponed_today"):
            postponed_today += 1

    return {
        "date_str": today_str,
        "done_count": len(done_today),
        "missed_count": len(missed_today),
        "postponed_count": postponed_today,
        "longest": longest,
    }

def fmt_duration(seconds: float) -> str:
    total_min = int(seconds // 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"

async def update_streak(uid: int, missed_count: int) -> int:
    state = await get_user_state(uid)
    today_str = datetime.now().strftime("%d.%m.%Y")
    if state.get("last_streak_date") == today_str:
        return state.get("streak", 0)
    streak = state.get("streak", 0)
    if missed_count == 0:
        streak += 1
    else:
        streak = 0
    await save_user_state(uid, {"streak": streak, "last_streak_date": today_str})
    return streak

async def get_streak(uid: int) -> int:
    state = await get_user_state(uid)
    return state.get("streak", 0)

# =========================================================
# GOALS (довгострокові цілі користувача)
# =========================================================

async def get_active_goals(uid: int) -> list:
    cursor = goals_col.find({"uid": uid, "active": True})
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []

async def get_all_goals(uid: int) -> list:
    cursor = goals_col.find({"uid": uid}).sort("created_at", -1)
    return await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []

async def add_goal(uid: int, title: str, description: str, priority: str):
    await db_call(goals_col.insert_one({
        "uid": uid,
        "title": title,
        "description": description,
        "priority": priority,
        "active": True,
        "created_at": datetime.now().isoformat(),
    }))

async def toggle_goal(gid: str, active: bool):
    await db_call(goals_col.update_one({"_id": ObjectId(gid)}, {"$set": {"active": active}}))

async def delete_goal(gid: str):
    await db_call(goals_col.delete_one({"_id": ObjectId(gid)}))

def fmt_goals_list(goals: list) -> str:
    if not goals:
        return "🎯 *Мої цілі*\n\nЩе немає жодної цілі.\nНатисни «➕ Додати ціль», щоб задати першу — AI буде враховувати її при плануванні."
    lines = ["🎯 *Мої цілі*", ""]
    for g in goals:
        status = "✅" if g.get("active") else "⏸ (неактивна)"
        pr = PRIORITY_EMOJI.get(g.get("priority", "medium"), "🟡")
        lines.append(f"{pr} *{g.get('title','')}* — {status}")
        if g.get("description"):
            lines.append(f"   _{g['description'][:80]}_")
        lines.append("")
    return "\n".join(lines).strip()

def ikb_goals(goals: list) -> InlineKeyboardMarkup:
    rows = []
    for g in goals:
        gid = str(g["_id"])
        toggle_text = "⏸ Деактивувати" if g.get("active") else "▶️ Активувати"
        title = g.get("title", "")[:24]
        rows.append([InlineKeyboardButton(text=f"🎯 {title}", callback_data="noop")])
        rows.append([
            InlineKeyboardButton(text=toggle_text, callback_data=f"goaltoggle:{gid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"goaldel:{gid}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Додати ціль", callback_data="goal_add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ai_menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_priority() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Високий"), KeyboardButton(text="🟡 Середній"), KeyboardButton(text="🟢 Низький")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def priority_from_text(text: str) -> str | None:
    return {"🔴 Високий": "high", "🟡 Середній": "medium", "🟢 Низький": "low"}.get(text)

# =========================================================
# AI ПЛАНЕР (OpenAI)
# =========================================================
# ai_suggestions_cache[uid] = {"plan": {"focus", "reason", "tasks": [...]}, "selected": set[int]}
ai_suggestions_cache: dict = {}

def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```")
    return raw.strip()

async def generate_ai_plan(uid: int) -> dict | None:
    """Аналізує активні задачі, виконані сьогодні, прострочені, streak/XP та активні
    цілі користувача і повертає структурований план: {"focus","reason","tasks":[...]}."""
    if not openai_client:
        return None
    try:
        active = sort_tasks(await get_user_tasks(uid, statuses=[STATUS_PENDING]))[:20]
        done_all = await get_user_tasks(uid, statuses=[STATUS_DONE])
        today_key = datetime.now().strftime("%Y-%m-%d")
        done_today = [t for t in done_all if (t.get("completed_at") or "").startswith(today_key)]
        overdue = [t for t in active if is_missed(t)]
        goals = await get_active_goals(uid)
        st = await get_user_state(uid)

        active_text = "\n".join(
            f"- {t.get('text','')} | {t.get('due','')} | "
            f"{LABELS.get(t.get('label',''), {}).get('name','')} | "
            f"{CATEGORIES.get(t.get('category',''), {}).get('name','')}"
            for t in active
        ) or "(активних задач немає)"

        done_text = "\n".join(f"- {t.get('text','')}" for t in done_today) or "(ще нічого не виконано)"
        goals_text = "\n".join(
            f"- [{g.get('priority','medium')}] {g.get('title','')}" + (f" — {g.get('description','')[:80]}" if g.get("description") else "")
            for g in goals
        ) or "(довгострокові цілі не задані)"

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

        prompt = f"""Ти — персональний AI-планувальник задач. Відповідай виключно українською.

Поточний час: {now_str}
Робочий графік користувача (основна робота): {WORK_HOURS_TEXT}

Активні задачі користувача:
{active_text}

Вже виконано сьогодні:
{done_text}

Прострочених активних задач: {len(overdue)}
Серія (streak): {st.get('streak', 0)} днів
XP: {st.get('xp', 0)}

Довгострокові цілі користувача (від найважливіших):
{goals_text}

Запропонуй 3-6 НОВИХ конкретних задач на сьогодні, реалістичних для виконання за день.
Правила:
- Не дублюй активні задачі (якщо задача вже покриває дію — не пропонуй схожу).
- Задачі мають бути конкретними ("Додати 1 товар з фото й ціною у Telegram"), а не абстрактними ("Попрацювати над бізнесом").
- Врахуй робочий графік: задачі про IT/навчання/пошук роботи став до початку або після завершення робочого дня, якщо немає інших вказівок.
- Не став дві задачі на однаковий час і не став задачу поверх уже запланованої активної задачі.
- Балансуй між напрямками (бізнес, IT/розвиток, поточні справи) — не роби весь план лише про одне.
- Враховуй пріоритет цілей (high > medium > low) при виборі фокуса дня.

Поверни ВИКЛЮЧНО валідний JSON без жодного тексту навколо, у форматі:
{{
  "focus": "короткий головний фокус дня",
  "reason": "одне речення чому саме такий фокус",
  "tasks": [
    {{"text": "конкретна дія", "label": "urgent|medium|low|idea|personal", "category": "work|finance|home|sport|study|other", "time": "гг:хх", "estimated_minutes": 30}}
  ]
}}"""

        resp = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        raw = _strip_json_fence(resp.choices[0].message.content or "")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("AI повернув не об'єкт JSON")

        tasks_out = []
        for it in data.get("tasks", []):
            if not isinstance(it, dict):
                continue
            label = it.get("label") if it.get("label") in LABELS else "medium"
            category = it.get("category") if it.get("category") in CATEGORIES else "other"
            time_str = str(it.get("time", "12:00")).strip()
            try:
                datetime.strptime(time_str, "%H:%M")
            except ValueError:
                time_str = "12:00"
            try:
                est = int(it.get("estimated_minutes", 30))
            except (TypeError, ValueError):
                est = 30
            est = max(5, min(est, 240))
            text = str(it.get("text") or "").strip()[:200]
            if not text:
                continue
            tasks_out.append({
                "text": text, "label": label, "category": category,
                "time": time_str, "estimated_minutes": est,
            })

        if not tasks_out:
            return None

        return {
            "focus": str(data.get("focus", "")).strip()[:120],
            "reason": str(data.get("reason", "")).strip()[:250],
            "tasks": tasks_out[:6],
        }
    except (json.JSONDecodeError, ValueError):
        logger.exception("AI Планер: не вдалося розпарсити відповідь OpenAI")
        return None
    except Exception:
        logger.exception("generate_ai_plan failed для uid=%s", uid)
        return None

async def generate_ai_analysis(uid: int) -> str | None:
    """Короткий текстовий аналіз продуктивності за сьогодні (для кнопки і для вечірнього AI-підсумку)."""
    if not openai_client:
        return None
    try:
        stats = await compute_daily_stats(uid)
        st = await get_user_state(uid)
        prompt = f"""Ти — персональний AI-аналітик продуктивності. Відповідай українською, коротко (до 120 слів), без формальних компліментів.

Дані за сьогодні:
Виконано задач: {stats['done_count']}
Пропущено (прострочено): {stats['missed_count']}
Перенесено на пізніше: {stats['postponed_count']}
Серія (streak): {st.get('streak', 0)} днів
XP: {st.get('xp', 0)}

Дай короткий, конкретний і корисний аналіз у форматі (українською, збережи ці підзаголовки):
🎯 Найкраще: ...
⚠️ Проблема: ...
💡 Що змінити завтра: ..."""
        resp = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        logger.exception("generate_ai_analysis failed для uid=%s", uid)
        return None

def fmt_ai_plan_preview(plan: dict, selected: set) -> str:
    tasks = plan.get("tasks", [])
    total_minutes = sum(t.get("estimated_minutes", 30) for i, t in enumerate(tasks) if i in selected)
    lines = ["☀️ *AI План на сьогодні*", ""]
    if plan.get("focus"):
        lines.append(f"🎯 Головний фокус: *{plan['focus']}*")
    if plan.get("reason"):
        lines.append(f"_{plan['reason']}_")
    lines.append("")
    lines.append("🔥 *Пріоритети:*")
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        label = LABELS.get(t["label"], {})
        cat = CATEGORIES.get(t["category"], {})
        lines.append(
            f"{mark} {label.get('emoji','')} *{t['time']}* — {t['text']} "
            f"({cat.get('emoji','')} {cat.get('name','')}, ~{t.get('estimated_minutes', 30)} хв)"
        )
    h, m = divmod(total_minutes, 60)
    load_parts = ([f"{h} год"] if h else []) + ([f"{m} хв"] if m else [])
    lines.append("")
    lines.append(f"📊 Заплановане навантаження: ~{' '.join(load_parts) or '0 хв'}")
    lines.append("")
    lines.append("Натисни на задачу, щоб зняти/додати позначку, потім підтверди.")
    return "\n".join(lines)

def ikb_ai_plan_preview(tasks: list, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(tasks):
        mark = "☑️" if i in selected else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {t['text'][:35]}", callback_data=f"aiptoggle:{i}")])
    rows.append([
        InlineKeyboardButton(text="🚀 Створити план", callback_data="aip_add"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data="aip_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_ai_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☀️ План на сьогодні", callback_data="ai_plan")],
        [InlineKeyboardButton(text="📊 Аналіз продуктивності", callback_data="ai_analysis")],
        [InlineKeyboardButton(text="🎯 Мої цілі", callback_data="ai_goals")],
        [InlineKeyboardButton(text="⚙️ Налаштування AI", callback_data="ai_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ai_close")],
    ])

def ikb_ai_settings(morning_enabled: bool) -> InlineKeyboardMarkup:
    label = "🔔 Ранковий план: Увімкнено (для мене)" if morning_enabled else "🔕 Ранковий план: Вимкнено (для мене)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data="ai_toggle_morning")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ai_menu_back")],
    ])

class Auth(StatesGroup):
    waiting_password = State()

class AddTask(StatesGroup):
    text = State()
    label = State()
    category = State()
    date = State()
    date_manual = State()
    time = State()

class EditField(StatesGroup):
    typing = State()

class AddGoal(StatesGroup):
    title = State()
    description = State()
    priority = State()

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Нове завдання"), KeyboardButton(text="📋 Сьогодні")],
        [KeyboardButton(text="📆 Всі активні"), KeyboardButton(text="⭐ Обране")],
        [KeyboardButton(text="🏷 Категорії"), KeyboardButton(text="📊 Dashboard")],
        [KeyboardButton(text="🤖 AI Планер"), KeyboardButton(text="📈 Статистика")],
        [KeyboardButton(text="🔥 Серія"), KeyboardButton(text="📦 Архів")],
        [KeyboardButton(text="📖 Підсумок дня"), KeyboardButton(text="📤 Експорт CSV")],
    ], resize_keyboard=True)

def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)

def kb_label() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Терміново"), KeyboardButton(text="🟡 Середньо")],
        [KeyboardButton(text="🟢 Не поспішає"), KeyboardButton(text="🔵 Ідея")],
        [KeyboardButton(text="🟣 Особисте")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def label_from_text(text: str) -> str | None:
    mapping = {
        "🔴 Терміново": "urgent", "🟡 Середньо": "medium", "🟢 Не поспішає": "low",
        "🔵 Ідея": "idea", "🟣 Особисте": "personal",
    }
    return mapping.get(text)

def kb_category() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💻 Робота"), KeyboardButton(text="💰 Фінанси")],
        [KeyboardButton(text="🏠 Дім"), KeyboardButton(text="💪 Спорт")],
        [KeyboardButton(text="📚 Навчання"), KeyboardButton(text="🗂 Інше")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def category_from_text(text: str) -> str | None:
    mapping = {
        "💻 Робота": "work", "💰 Фінанси": "finance", "🏠 Дім": "home",
        "💪 Спорт": "sport", "📚 Навчання": "study", "🗂 Інше": "other",
    }
    return mapping.get(text)

def kb_date() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Сьогодні"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="✏️ Своя дата (дд.мм.рррр)")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def ikb_task_actions(tid: int, t: dict) -> InlineKeyboardMarkup:
    if t.get("pinned"):
        pin_btn = InlineKeyboardButton(text="📌 Відкріпити", callback_data=f"unpin:{tid}")
    else:
        pin_btn = InlineKeyboardButton(text="⭐ Закріпити", callback_data=f"pin:{tid}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [pin_btn, InlineKeyboardButton(text="📝 Підзадачі", callback_data=f"subtasks:{tid}")],
        [
            InlineKeyboardButton(text="🕐 +1 год", callback_data=f"postp1h:{tid}"),
            InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"edit:{tid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"deltask:{tid}"),
        ],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="back_to_list")],
    ])

def ikb_rollover_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [InlineKeyboardButton(text="🕐 Через 1 год", callback_data=f"postp1h:{tid}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}")],
        [InlineKeyboardButton(text="❌ Видалити", callback_data=f"deltask:{tid}")],
    ])

def ikb_reminder_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
    ])

def ikb_edit_fields(tid: int) -> InlineKeyboardMarkup:
    fields = [
        ("text", "📝 Текст"),
        ("label", "🎨 Мітка"),
        ("category", "🏷 Категорія"),
        ("date", "📅 Дата"),
        ("time", "🕐 Час"),
        ("subtasks_add", "➕ Додати підзадачі"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"editfield:{tid}:{key}")] for key, label in fields]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_tasks_list(tasks: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = tasks[start:start + per_page]
    rows = []
    for t in chunk:
        label = LABELS.get(t.get("label", "idea"), {"emoji": ""})
        status_icon = "✅" if t.get("status") == STATUS_DONE else ("⚠️" if is_missed(t) else "⏳")
        pin_str = "📌" if t.get("pinned") else ""
        lbl = f"{status_icon} {pin_str}{label['emoji']} №{t['id']} {t.get('due','')[-5:]} {t.get('text','')[:18]}"
        rows.append([InlineKeyboardButton(text=lbl, callback_data=f"view:{t['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page-1}"))
    total_pages = max(1, (len(tasks) - 1) // per_page + 1)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if start + per_page < len(tasks):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_categories() -> InlineKeyboardMarkup:
    rows = []
    for key, cat in CATEGORIES.items():
        rows.append([InlineKeyboardButton(text=f"{cat['emoji']} {cat['name']}", callback_data=f"catopen:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_archive_clear() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так", callback_data="archclr:yes"),
            InlineKeyboardButton(text="❌ Ні", callback_data="archclr:no"),
        ],
    ])

def render_subtasks(tid: int, t: dict):
    subtasks = t.get("subtasks") or []
    if not subtasks:
        text = f"📝 *Підзадачі* — №{tid}\n\nПоки що немає підзадач.\nДодай їх через ✏️ Редагувати → ➕ Додати підзадачі."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")]])
        return text, kb
    rows = []
    for s in subtasks:
        box = "☑" if s.get("done") else "☐"
        rows.append([InlineKeyboardButton(text=f"{box} {s['text'][:40]}", callback_data=f"subtoggle:{tid}:{s['id']}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    done_n = sum(1 for s in subtasks if s.get("done"))
    text = f"📝 *Підзадачі* — №{tid}\n\n{done_n}/{len(subtasks)} виконано"
    return text, kb

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher(storage=MemoryStorage())

user_list_cache: dict = {}

async def require_auth(msg: Message, state: FSMContext) -> bool:
    try:
        authed = await is_authorized(msg.from_user.id)
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT)
        return False
    if authed:
        return True
    current_state = await state.get_state()
    if current_state != Auth.waiting_password:
        await state.set_state(Auth.waiting_password)
        await msg.answer("🔒 *Доступ закритий*\n\nВведіть пароль:", reply_markup=ReplyKeyboardRemove())
    return False

@dp.errors()
async def global_error_handler(event, exception=None):
    exc = exception if exception is not None else getattr(event, "exception", None)
    logger.exception("Unhandled error while processing update: %s", exc)
    update = getattr(event, "update", None)
    chat_id = None
    try:
        if update and update.message:
            chat_id = update.message.chat.id
        elif update and update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
    except Exception:
        chat_id = None
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, DB_ERROR_TEXT, reply_markup=kb_main())
        except Exception:
            pass
    return True

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    try:
        authed = await is_authorized(msg.from_user.id)
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT)
        return
    if authed:
        await msg.answer("👋 *Менеджер завдань*\n\nОбери дію:", reply_markup=kb_main())
    else:
        await state.set_state(Auth.waiting_password)
        await msg.answer("🔒 *Доступ закритий*\n\nВведіть пароль:", reply_markup=ReplyKeyboardRemove())

@dp.message(Auth.waiting_password)
async def check_password(msg: Message, state: FSMContext):
    if msg.text == BOT_PASSWORD:
        try:
            await authorize(msg.from_user.id)
        except DBUnavailable:
            await msg.answer(DB_ERROR_TEXT)
            return
        await state.clear()
        await msg.answer("✅ *Пароль вірний! Ласкаво просимо.*\n\nОбери дію:", reply_markup=kb_main())
    else:
        await msg.answer("❌ Невірний пароль. Спробуй ще раз:")

@dp.message(F.text == "➕ Нове завдання")
async def new_task_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    await state.set_state(AddTask.text)
    await msg.answer("📝 Введіть *текст завдання*:", reply_markup=kb_cancel())

@dp.message(AddTask.text)
async def at_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(text=msg.text.strip())
    await state.set_state(AddTask.label)
    await msg.answer("🎨 Оберіть *мітку*:", reply_markup=kb_label())

@dp.message(AddTask.label)
async def at_label(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    label = label_from_text(msg.text)
    if not label:
        return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_label())
    await state.update_data(label=label)
    await state.set_state(AddTask.category)
    await msg.answer("🏷 Оберіть *категорію*:", reply_markup=kb_category())

@dp.message(AddTask.category)
async def at_category(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    category = category_from_text(msg.text)
    if not category:
        return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_category())
    await state.update_data(category=category)
    await state.set_state(AddTask.date)
    await msg.answer("📅 Коли треба це зробити? Оберіть дату:", reply_markup=kb_date())

@dp.message(AddTask.date)
async def at_date(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    if msg.text == "✏️ Своя дата (дд.мм.рррр)":
        await state.set_state(AddTask.date_manual)
        return await msg.answer("📅 Введіть дату як *дд.мм.рррр*:", reply_markup=kb_cancel())
    today = datetime.now()
    if msg.text == "📅 Сьогодні":
        date_str = today.strftime("%d.%m.%Y")
    elif msg.text == "📅 Завтра":
        date_str = (today + timedelta(days=1)).strftime("%d.%m.%Y")
    else:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_date())
    await _at_date_save(msg, state, date_str)

@dp.message(AddTask.date_manual)
async def at_date_manual(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    try:
        datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return await msg.answer(
            "⚠️ Невірний формат. Введіть дату як *дд.мм.рррр*\n_Наприклад: 10.10.2025_",
            reply_markup=kb_cancel()
        )
    await _at_date_save(msg, state, raw)

async def _at_date_save(msg: Message, state: FSMContext, date_str: str):
    await state.update_data(date=date_str)
    await state.set_state(AddTask.time)
    await msg.answer(f"✅ Дата: *{date_str}*\n\n🕐 Введіть *час* як *гг:хх*:\n_Наприклад: 18:30_",
                     reply_markup=kb_cancel())

@dp.message(AddTask.time)
async def at_time(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    raw = msg.text.strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        return await msg.answer(
            "⚠️ Невірний формат. Введіть час як *гг:хх*\n_Наприклад: 18:30_",
            reply_markup=kb_cancel()
        )
    fd = await state.get_data()
    date_str = fd["date"]
    due_dt = datetime.strptime(f"{date_str} {raw}", "%d.%m.%Y %H:%M")
    await state.clear()

    created_at = datetime.now().isoformat()
    try:
        new_id = await next_task_id()
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
        return
    task = {
        "id": new_id,
        "uid": msg.from_user.id,
        "text": fd["text"],
        "label": fd["label"],
        "category": fd["category"],
        "due": fmt_due(due_dt),
        "status": STATUS_PENDING,
        "pinned": False,
        "subtasks": [],
        "created_at": created_at,
        "completed_at": None,
        "reminded_before": False,
        "missed_flagged": False,
        "missed_counted": False,
        "postponed_count": 0,
        "postponed_today": False,
        "source": "manual",
    }

    try:
        await add_task(task)
        saved = await get_task(task["id"])
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
        return
    if not saved:
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
        return
    await msg.answer(f"✅ *Завдання додано!*\n\n{fmt_task(saved)}", reply_markup=kb_main())

@dp.message(F.text == "📋 Сьогодні")
async def today_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        uid = msg.from_user.id
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_DONE])
        tasks = [t for t in tasks if is_today(t.get("due", ""))]
        tasks = sort_tasks_by_label_then_due(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 На сьогодні завдань немає.", reply_markup=kb_main())
        await msg.answer(f"📋 *Сьогодні* — {len(tasks)} шт.", reply_markup=kb_main())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("today_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

@dp.message(F.text == "📆 Всі активні")
async def active_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        uid = msg.from_user.id
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = sort_tasks(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 Активних завдань немає.", reply_markup=kb_main())
        await msg.answer(f"📆 *Всі активні* — {len(tasks)} шт.", reply_markup=kb_main())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("active_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

@dp.message(F.text == "⭐ Обране")
async def favorites_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        uid = msg.from_user.id
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = [t for t in tasks if t.get("pinned")]
        tasks = sort_tasks(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("⭐ *Обране*\n\n📭 Немає закріплених завдань.", reply_markup=kb_main())
        await msg.answer(f"⭐ *Важливі* — {len(tasks)} шт.", reply_markup=kb_main())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("favorites_view failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

@dp.message(F.text == "🏷 Категорії")
async def categories_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    await msg.answer("🏷 *Оберіть категорію:*", reply_markup=ikb_categories())

@dp.callback_query(F.data.startswith("catopen:"))
async def category_open(cb: CallbackQuery):
    try:
        key = cb.data.split(":")[1]
        cat = CATEGORIES.get(key)
        if not cat:
            return await cb.answer("Невідома категорія", show_alert=True)
        uid = cb.from_user.id
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING])
        tasks = [t for t in tasks if t.get("category") == key]
        tasks = sort_tasks_by_label_then_due(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            await cb.message.edit_text(f"{cat['emoji']} *{cat['name']}*\n\n📭 Немає завдань у цій категорії.")
        else:
            await cb.message.edit_text(f"{cat['emoji']} *{cat['name']}* — {len(tasks)} шт.", reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("category_open failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("page:"))
async def page_tasks(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        page = int(cb.data.split(":")[1])
        tasks = user_list_cache.get(uid) or []
        await cb.message.edit_reply_markup(reply_markup=ikb_tasks_list(tasks, page))
        await cb.answer()
    except Exception:
        logger.exception("page_tasks failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data == "back_to_list")
async def back_to_list(cb: CallbackQuery):
    try:
        uid = cb.from_user.id
        tasks = user_list_cache.get(uid) or []
        if not tasks:
            await cb.message.edit_text("📭 Список порожній.")
            return await cb.answer()
        await cb.message.edit_text("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
        await cb.answer()
    except Exception:
        logger.exception("back_to_list failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()

@dp.callback_query(F.data.startswith("view:"))
async def view_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer()
    except Exception:
        logger.exception("view_task failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("done:"))
async def task_done(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)

        state = await get_user_state(t["uid"])
        old_xp = state.get("xp", 0)
        gain = LABEL_XP.get(t.get("label", "idea"), 10)
        new_xp = old_xp + gain
        old_level, _, _ = level_progress(old_xp)
        new_level, _, _ = level_progress(new_xp)

        await update_task(tid, {"status": STATUS_DONE, "completed_at": datetime.now().isoformat()})
        await save_user_state(t["uid"], {
            "xp": new_xp,
            "total_completed": state.get("total_completed", 0) + 1,
        })
        t = await get_task(tid)

        extra = f"\n\n✨ +{gain} XP"
        if new_level > old_level:
            extra += f"\n🏆 Новий рівень: *{new_level}*!"

        try:
            await cb.message.edit_text(f"✅ *Виконано!*\n\n{fmt_task(t)}{extra}")
        except TelegramAPIError:
            await cb.message.answer(f"✅ *Виконано!*\n\n{fmt_task(t)}{extra}")
        await cb.answer("✅ Виконано!")
    except Exception:
        logger.exception("task_done failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

async def _postpone(cb: CallbackQuery, tid: int, new_due: datetime):
    t = await get_task(tid)
    if not t:
        return await cb.answer("Не знайдено!", show_alert=True)
    await update_task(tid, {
        "due": fmt_due(new_due),
        "status": STATUS_PENDING,
        "reminded_before": False,
        "missed_flagged": False,
        "postponed_count": t.get("postponed_count", 0) + 1,
        "postponed_today": True,
    })
    state = await get_user_state(t["uid"])
    await save_user_state(t["uid"], {"total_postponed": state.get("total_postponed", 0) + 1})
    t = await get_task(tid)
    try:
        await cb.message.edit_text(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid, t))
    except TelegramAPIError:
        await cb.message.answer(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid, t))
    await cb.answer("🔁 Перенесено")

@dp.callback_query(F.data.startswith("postp1h:"))
async def postpone_1h(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await _postpone(cb, tid, datetime.now() + timedelta(hours=1))
    except Exception:
        logger.exception("postpone_1h failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("postptom:"))
async def postpone_tomorrow(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        old_due = parse_due(t.get("due", "")) or datetime.now()
        new_due = (datetime.now() + timedelta(days=1)).replace(
            hour=old_due.hour, minute=old_due.minute, second=0, microsecond=0
        )
        await _postpone(cb, tid, new_due)
    except Exception:
        logger.exception("postpone_tomorrow failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("deltask:"))
async def delete_task_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        await delete_task(tid)
        await cb.message.edit_text("🗑 Завдання видалено.")
        await cb.answer("Видалено!")
    except Exception:
        logger.exception("delete_task_cb failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("pin:"))
async def pin_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await update_task(tid, {"pinned": True})
        t = await get_task(tid)
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer("⭐ Закріплено!")
    except Exception:
        logger.exception("pin_task failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("unpin:"))
async def unpin_task(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await update_task(tid, {"pinned": False})
        t = await get_task(tid)
        await cb.message.edit_text(fmt_task(t), reply_markup=ikb_task_actions(tid, t))
        await cb.answer("📌 Відкріплено")
    except Exception:
        logger.exception("unpin_task failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("subtasks:"))
async def subtasks_view(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        text, kb = render_subtasks(tid, t)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("subtasks_view failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("subtoggle:"))
async def subtask_toggle(cb: CallbackQuery):
    try:
        _, tid_s, subid = cb.data.split(":")
        tid = int(tid_s)
        t = await get_task(tid)
        if not t:
            return await cb.answer("Не знайдено!", show_alert=True)
        subtasks = t.get("subtasks") or []
        for s in subtasks:
            if s["id"] == subid:
                s["done"] = not s.get("done")
        await update_task(tid, {"subtasks": subtasks})
        t = await get_task(tid)
        text, kb = render_subtasks(tid, t)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("subtask_toggle failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("edit:"))
async def edit_task_cb(cb: CallbackQuery):
    try:
        tid = int(cb.data.split(":")[1])
        await cb.message.edit_text(f"✏️ *Редагування №{tid}*\nОберіть поле:", reply_markup=ikb_edit_fields(tid))
        await cb.answer()
    except Exception:
        logger.exception("edit_task_cb failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("editfield:"))
async def edit_field_choose(cb: CallbackQuery, state: FSMContext):
    try:
        _, tid_s, field = cb.data.split(":", 2)
        tid = int(tid_s)
        labels = {
            "text": "Текст завдання",
            "label": "Мітка",
            "category": "Категорія",
            "date": "Дата (дд.мм.рррр)",
            "time": "Час (гг:хх)",
            "subtasks_add": "Нові підзадачі (кожна з нового рядка)",
        }
        await state.set_state(EditField.typing)
        await state.update_data(edit_tid=tid, edit_field=field)
        if field == "label":
            kb = kb_label()
        elif field == "category":
            kb = kb_category()
        else:
            kb = kb_cancel()
        await cb.message.answer(f"Введіть нове значення для *{labels.get(field, field)}*:", reply_markup=kb)
        await cb.answer()
    except Exception:
        logger.exception("edit_field_choose failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.message(EditField.typing)
async def edit_field_save(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    fd = await state.get_data()
    tid, field = fd["edit_tid"], fd["edit_field"]
    t = await get_task(tid)
    if not t:
        await state.clear()
        return await msg.answer("Завдання не знайдено.", reply_markup=kb_main())

    if field == "text":
        await update_task(tid, {"text": msg.text.strip()})
    elif field == "label":
        label = label_from_text(msg.text)
        if not label:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_label())
        await update_task(tid, {"label": label})
    elif field == "category":
        category = category_from_text(msg.text)
        if not category:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_category())
        await update_task(tid, {"category": category})
    elif field == "date":
        try:
            datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *дд.мм.рррр*:", reply_markup=kb_cancel())
        old_due = parse_due(t.get("due", ""))
        time_part = old_due.strftime("%H:%M") if old_due else "00:00"
        new_due = f"{msg.text.strip()} {time_part}"
        await update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                 "reminded_before": False, "missed_flagged": False})
    elif field == "time":
        try:
            datetime.strptime(msg.text.strip(), "%H:%M")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *гг:хх*:", reply_markup=kb_cancel())
        old_due = parse_due(t.get("due", ""))
        date_part = old_due.strftime("%d.%m.%Y") if old_due else datetime.now().strftime("%d.%m.%Y")
        new_due = f"{date_part} {msg.text.strip()}"
        await update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                 "reminded_before": False, "missed_flagged": False})
    elif field == "subtasks_add":
        subtasks = t.get("subtasks") or []
        added = 0
        for line in msg.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            subtasks.append({"id": secrets.token_hex(2), "text": line, "done": False})
            added += 1
        await update_task(tid, {"subtasks": subtasks})
        if added == 0:
            return await msg.answer("⚠️ Не знайшов жодного рядка з текстом. Спробуй ще раз:", reply_markup=kb_cancel())

    await state.clear()
    t = await get_task(tid)
    if t:
        await msg.answer(f"✅ Оновлено!\n\n{fmt_task(t)}", reply_markup=kb_main())
    else:
        await msg.answer("Завдання не знайдено.", reply_markup=kb_main())

@dp.message(F.text == "🔥 Серія")
async def streak_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        streak = await get_streak(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    lines = [
        "🔥 *Серія*", "",
        f"{streak} {'день' if streak % 10 == 1 and streak % 100 != 11 else 'днів'}",
        "без пропущених завдань" if streak > 0 else "почни виконувати завдання без пропусків!",
    ]
    await msg.answer("\n".join(lines), reply_markup=kb_main())

@dp.message(F.text == "📦 Архів")
async def archive_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    uid = msg.from_user.id
    try:
        tasks = await get_user_tasks(uid, statuses=[STATUS_DONE])
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not tasks:
        return await msg.answer("📦 *Архів*\n\nВиконаних завдань ще немає.", reply_markup=kb_main())
    by_month: dict = {}
    for t in tasks:
        ca = t.get("completed_at") or t.get("created_at")
        try:
            dt = datetime.fromisoformat(ca)
        except (ValueError, TypeError):
            continue
        key = (dt.year, dt.month)
        by_month[key] = by_month.get(key, 0) + 1
    lines = ["📦 *Архів виконаних завдань*", ""]
    for (year, month) in sorted(by_month.keys()):
        lines.append(f"{MONTHS_UA[month-1]} {year}")
        lines.append(f"✔ {by_month[(year, month)]} задач{'а' if by_month[(year, month)] == 1 else ''}")
        lines.append("")
    await msg.answer("\n".join(lines).strip(), reply_markup=kb_main())

@dp.message(F.text == "📖 Підсумок дня")
async def summary_on_demand(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        stats = await compute_daily_stats(msg.from_user.id)
        streak = await get_streak(msg.from_user.id)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await msg.answer(build_daily_summary_text(stats, streak), reply_markup=kb_main())

def build_daily_summary_text(stats: dict, streak: int) -> str:
    lines = [
        f"📅 *Підсумок дня — {stats['date_str']}*", "",
        f"✅ Виконано\n{stats['done_count']} задач", "",
        f"❌ Не виконано\n{stats['missed_count']}", "",
    ]
    if stats["longest"]:
        seconds, text = stats["longest"]
        lines.append(f"⏱ Найдовша задача\n{fmt_duration(seconds)} ({text[:30]})")
        lines.append("")
    lines.append(f"🔥 Серія\n{streak} днів")
    if stats["postponed_count"]:
        lines.append("")
        lines.append(f"↪️ Перенесено на пізніше\n{stats['postponed_count']} задач")
    return "\n".join(lines)

@dp.message(F.text == "📊 Dashboard")
async def dashboard_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        uid = msg.from_user.id
        tasks = await get_user_tasks(uid)
        active = [t for t in tasks if t.get("status") == STATUS_PENDING]
        done_all = [t for t in tasks if t.get("status") == STATUS_DONE]
        today_str = datetime.now().strftime("%Y-%m-%d")
        done_today = [t for t in done_all if (t.get("completed_at") or "").startswith(today_str)]
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        tomorrow_tasks = [t for t in active if t.get("due", "").startswith(tomorrow_str)]
        overdue_tasks = [t for t in active if is_missed(t)]
        important = [t for t in active if t.get("pinned")]

        state_doc = await get_user_state(uid)
        streak = state_doc.get("streak", 0)
        total_completed = state_doc.get("total_completed", 0)
        total_missed = state_doc.get("total_missed", 0)
        denom = total_completed + total_missed
        completion = round(total_completed / denom * 100) if denom > 0 else 100

        xp = state_doc.get("xp", 0)
        level, into_level, threshold = level_progress(xp)
        bar_len = 12
        filled = int(bar_len * into_level / threshold) if threshold else 0
        bar = "█" * filled + "░" * (bar_len - filled)

        name = msg.from_user.first_name or msg.from_user.username or "Друже"
        text = (
            f"👤 *{name}*\n\n"
            "━━━━━━━━━━━━\n\n"
            f"📋 Активних\n*{len(active)}*\n\n"
            f"🔥 Серія\n*{streak} днів*\n\n"
            f"✅ Виконано сьогодні\n*{len(done_today)}*\n\n"
            f"📅 На завтра\n*{len(tomorrow_tasks)}*\n\n"
            f"⏰ Прострочено\n*{len(overdue_tasks)}*\n\n"
            f"⭐ Важливих\n*{len(important)}*\n\n"
            f"📦 Архів\n*{len(done_all)}*\n\n"
            f"📈 Виконання\n*{completion}%*\n\n"
            "━━━━━━━━━━━━\n\n"
            f"🏆 Level {level}\n"
            f"{bar}\n"
            f"{into_level} / {threshold} XP"
        )
        await msg.answer(text, reply_markup=kb_main())
    except DBUnavailable:
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    except Exception:
        logger.exception("dashboard_view failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

@dp.message(F.text == "📈 Статистика")
async def stats_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    uid = msg.from_user.id
    try:
        st = await get_user_state(uid)
        tasks = await get_user_tasks(uid)
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

    total_completed = st.get("total_completed", 0)
    total_missed = st.get("total_missed", 0)
    total_postponed = st.get("total_postponed", 0)
    denom = total_completed + total_missed
    completion = round(total_completed / denom * 100) if denom else 100

    today = datetime.now().date()
    by_day = {today - timedelta(days=i): {"done": 0, "missed": 0} for i in range(7)}

    for t in tasks:
        if t.get("status") == STATUS_DONE and t.get("completed_at"):
            try:
                d = datetime.fromisoformat(t["completed_at"]).date()
            except ValueError:
                continue
            if d in by_day:
                by_day[d]["done"] += 1
        elif is_missed(t):
            due = parse_due(t.get("due", ""))
            if due and due.date() in by_day:
                by_day[due.date()]["missed"] += 1

    active = [t for t in tasks if t.get("status") == STATUS_PENDING]
    by_cat: dict = {}
    for t in active:
        key = t.get("category", "other")
        by_cat[key] = by_cat.get(key, 0) + 1

    lines = [
        "📈 *Статистика*", "",
        f"✅ Всього виконано: *{total_completed}*",
        f"❌ Всього пропущено: *{total_missed}*",
        f"🔁 Всього перенесено: *{total_postponed}*",
        f"📊 Відсоток виконання: *{completion}%*",
        "",
        "*Останні 7 днів:*",
    ]
    for d in sorted(by_day.keys()):
        stat = by_day[d]
        lines.append(f"{d.strftime('%d.%m')} — ✅ {stat['done']}  ❌ {stat['missed']}")

    if by_cat:
        lines.append("")
        lines.append("*Активні задачі за категоріями:*")
        for key, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
            cat = CATEGORIES.get(key, {"emoji": "", "name": key})
            lines.append(f"{cat['emoji']} {cat['name']} — {cnt}")

    await msg.answer("\n".join(lines), reply_markup=kb_main())

# =========================================================
# AI ПЛАНЕР — хендлери
# =========================================================

@dp.message(F.text == "🤖 AI Планер")
async def ai_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    if not openai_client:
        return await msg.answer(
            "⚠️ AI Планер не налаштований.\nДодай змінну середовища `OPENAI_API_KEY`, щоб увімкнути цю функцію.",
            reply_markup=kb_main(),
        )
    await msg.answer("🤖 *AI Планер*\n\nЩо зробити?", reply_markup=ikb_ai_menu())

@dp.callback_query(F.data == "ai_close")
async def ai_close_cb(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass
    await cb.answer()

@dp.callback_query(F.data == "ai_menu_back")
async def ai_menu_back_cb(cb: CallbackQuery):
    await cb.message.edit_text("🤖 *AI Планер*\n\nЩо зробити?", reply_markup=ikb_ai_menu())
    await cb.answer()

@dp.callback_query(F.data == "ai_plan")
async def ai_plan_cb(cb: CallbackQuery):
    if not openai_client:
        return await cb.message.edit_text(AI_ERROR_TEXT)
    try:
        await cb.answer("Генерую...")
        await cb.message.edit_text("☀️ Аналізую твої задачі, цілі та статистику, зачекай кілька секунд...")
        plan = await generate_ai_plan(cb.from_user.id)
        if not plan or not plan.get("tasks"):
            return await cb.message.edit_text(AI_ERROR_TEXT)
        selected = set(range(len(plan["tasks"])))
        ai_suggestions_cache[cb.from_user.id] = {"plan": plan, "selected": selected}
        await save_user_state(cb.from_user.id, {"last_ai_plan_date": datetime.now().strftime("%Y-%m-%d")})
        await cb.message.edit_text(
            fmt_ai_plan_preview(plan, selected),
            reply_markup=ikb_ai_plan_preview(plan["tasks"], selected),
        )
    except Exception:
        logger.exception("ai_plan_cb failed")
        try:
            await cb.message.edit_text(AI_ERROR_TEXT)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("aiptoggle:"))
async def aiptoggle_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = ai_suggestions_cache.get(uid)
    if not data:
        return await cb.answer("Сесія застаріла, згенеруй план заново.", show_alert=True)
    idx = int(cb.data.split(":")[1])
    sel = data["selected"]
    if idx in sel:
        sel.discard(idx)
    else:
        sel.add(idx)
    plan = data["plan"]
    await cb.message.edit_text(fmt_ai_plan_preview(plan, sel), reply_markup=ikb_ai_plan_preview(plan["tasks"], sel))
    await cb.answer()

@dp.callback_query(F.data == "aip_add")
async def aip_add_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    data = ai_suggestions_cache.pop(uid, None)
    if not data or not data["selected"]:
        return await cb.answer("Нічого не обрано.", show_alert=True)
    tasks = data["plan"]["tasks"]
    today = datetime.now().strftime("%d.%m.%Y")
    added = 0
    try:
        for i in sorted(data["selected"]):
            it = tasks[i]
            new_id = await next_task_id()
            task = {
                "id": new_id,
                "uid": uid,
                "text": it["text"],
                "label": it["label"],
                "category": it["category"],
                "due": f"{today} {it['time']}",
                "status": STATUS_PENDING,
                "pinned": False,
                "subtasks": [],
                "created_at": datetime.now().isoformat(),
                "completed_at": None,
                "reminded_before": False,
                "missed_flagged": False,
                "missed_counted": False,
                "postponed_count": 0,
                "postponed_today": False,
                "source": "ai",
            }
            await add_task(task)
            added += 1
    except DBUnavailable:
        pass
    await cb.message.edit_text(f"✅ *Створено план!*\n\nДодано {added} задач(і) — вони вже в твоєму списку активних, з нагадуваннями та XP як завжди.")
    await cb.answer()

@dp.callback_query(F.data == "aip_cancel")
async def aip_cancel_cb(cb: CallbackQuery):
    ai_suggestions_cache.pop(cb.from_user.id, None)
    await cb.message.edit_text("❌ План відхилено. Задачі не додано.")
    await cb.answer()

@dp.callback_query(F.data == "ai_analysis")
async def ai_analysis_cb(cb: CallbackQuery):
    if not openai_client:
        return await cb.message.edit_text(AI_ERROR_TEXT)
    try:
        await cb.answer("Аналізую...")
        await cb.message.edit_text("📊 Аналізую твою продуктивність...")
        text = await generate_ai_analysis(cb.from_user.id)
        if not text:
            return await cb.message.edit_text(AI_ERROR_TEXT)
        await cb.message.edit_text(f"📊 *Аналіз продуктивності*\n\n{text}")
    except Exception:
        logger.exception("ai_analysis_cb failed")
        try:
            await cb.message.edit_text(AI_ERROR_TEXT)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data == "ai_goals")
async def ai_goals_cb(cb: CallbackQuery):
    try:
        goals = await get_all_goals(cb.from_user.id)
        await cb.message.edit_text(fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer()
    except DBUnavailable:
        await cb.message.edit_text(DB_ERROR_TEXT)

@dp.callback_query(F.data.startswith("goaltoggle:"))
async def goal_toggle_cb(cb: CallbackQuery):
    try:
        gid = cb.data.split(":", 1)[1]
        goals = await get_all_goals(cb.from_user.id)
        cur = next((g for g in goals if str(g["_id"]) == gid), None)
        if not cur:
            return await cb.answer("Не знайдено", show_alert=True)
        await toggle_goal(gid, not cur.get("active", True))
        goals = await get_all_goals(cb.from_user.id)
        await cb.message.edit_text(fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer("Оновлено")
    except Exception:
        logger.exception("goal_toggle_cb failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("goaldel:"))
async def goal_delete_cb(cb: CallbackQuery):
    try:
        gid = cb.data.split(":", 1)[1]
        await delete_goal(gid)
        goals = await get_all_goals(cb.from_user.id)
        await cb.message.edit_text(fmt_goals_list(goals), reply_markup=ikb_goals(goals))
        await cb.answer("Видалено")
    except Exception:
        logger.exception("goal_delete_cb failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

@dp.callback_query(F.data == "goal_add")
async def goal_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddGoal.title)
    await cb.message.answer("🎯 Введи назву цілі (коротко):", reply_markup=kb_cancel())
    await cb.answer()

@dp.message(AddGoal.title)
async def goal_add_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    await state.update_data(title=msg.text.strip()[:100])
    await state.set_state(AddGoal.description)
    await msg.answer("📝 Опиши ціль детальніше (або напиши «-», щоб пропустити):", reply_markup=kb_cancel())

@dp.message(AddGoal.description)
async def goal_add_desc(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    desc = "" if msg.text.strip() == "-" else msg.text.strip()[:300]
    await state.update_data(description=desc)
    await state.set_state(AddGoal.priority)
    await msg.answer("🎚 Обери пріоритет цілі:", reply_markup=kb_priority())

@dp.message(AddGoal.priority)
async def goal_add_priority(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    priority = priority_from_text(msg.text)
    if not priority:
        return await msg.answer("⚠️ Оберіть варіант на клавіатурі:", reply_markup=kb_priority())
    fd = await state.get_data()
    try:
        await add_goal(msg.from_user.id, fd["title"], fd.get("description", ""), priority)
    except DBUnavailable:
        await state.clear()
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    await state.clear()
    await msg.answer("✅ Ціль додано! AI буде враховувати її при плануванні.", reply_markup=kb_main())

@dp.callback_query(F.data == "ai_settings")
async def ai_settings_cb(cb: CallbackQuery):
    st = await get_user_state(cb.from_user.id)
    enabled = st.get("ai_morning_enabled", True)
    global_status = "увімкнено адміністратором" if AI_DAILY_PLAN_ENABLED else "вимкнено адміністратором (env AI_DAILY_PLAN_ENABLED=false)"
    text = (
        "⚙️ *Налаштування AI*\n\n"
        f"🕐 Час ранкового плану: *{AI_DAILY_PLAN_TIME}*\n"
        f"🌐 Глобально: {global_status}\n\n"
        "Можеш увімкнути/вимкнути ранковий план особисто для себе:"
    )
    await cb.message.edit_text(text, reply_markup=ikb_ai_settings(enabled))
    await cb.answer()

@dp.callback_query(F.data == "ai_toggle_morning")
async def ai_toggle_morning_cb(cb: CallbackQuery):
    st = await get_user_state(cb.from_user.id)
    new_val = not st.get("ai_morning_enabled", True)
    await save_user_state(cb.from_user.id, {"ai_morning_enabled": new_val})
    await cb.message.edit_reply_markup(reply_markup=ikb_ai_settings(new_val))
    await cb.answer("Оновлено!")

@dp.message(F.text == "📤 Експорт CSV")
async def export_csv(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    uid = msg.from_user.id
    try:
        tasks = sort_tasks(await get_user_tasks(uid))
    except DBUnavailable:
        return await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())
    if not tasks:
        return await msg.answer("📭 Немає завдань для експорту.", reply_markup=kb_main())
    output = io.StringIO()
    fieldnames = ["id", "text", "label", "category", "due", "status", "pinned", "completed_at", "postponed_count", "source"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for t in tasks:
        writer.writerow({f: t.get(f, "") for f in fieldnames})
    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"tasks_{datetime.now().strftime('%d%m%Y_%H%M')}.csv"
    await msg.answer_document(
        BufferedInputFile(csv_bytes, filename=filename),
        caption=f"📤 Експорт: *{len(tasks)}* завдань\n_{datetime.now().strftime('%d.%m.%Y %H:%M')}_"
    )

@dp.callback_query(F.data.startswith("archclr:"))
async def archive_clear_cb(cb: CallbackQuery):
    try:
        choice = cb.data.split(":")[1]
        uid = cb.from_user.id
        if choice == "yes":
            done_tasks = await get_user_tasks(uid, statuses=[STATUS_DONE])
            for t in done_tasks:
                await delete_task(t["id"])
            await cb.message.edit_text(f"🧹 Архів очищено ({len(done_tasks)} задач видалено).")
        else:
            await cb.message.edit_text("Гаразд, архів залишено без змін.")
        await cb.answer()
    except Exception:
        logger.exception("archive_clear_cb failed")
        try:
            await cb.answer(DB_ERROR_TEXT, show_alert=True)
        except TelegramAPIError:
            pass

async def reminder_task():
    while True:
        await asyncio.sleep(30)
        try:
            cursor = tasks_col.find({"status": STATUS_PENDING, "reminded_before": False}, {"_id": 0})
            pending = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
            now = datetime.now()
            for t in pending:
                due = parse_due(t.get("due", ""))
                if not due:
                    continue
                if due > now and due - now <= timedelta(minutes=REMINDER_BEFORE_MINUTES):
                    text = (
                        f"⏰ *Нагадування!*\n\n"
                        f"Через {REMINDER_BEFORE_MINUTES} хв: *{t.get('text','')}*\n"
                        f"{LABELS.get(t.get('label','idea'),{}).get('emoji','')} "
                        f"{LABELS.get(t.get('label','idea'),{}).get('name','')}\n"
                        f"🕐 {t.get('due','')}"
                    )
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_reminder_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send pre-reminder for task %s", t.get("id"))
                    await update_task(t["id"], {"reminded_before": True})
        except Exception:
            logger.exception("reminder_task loop failed")

async def midnight_rollover_task():
    while True:
        now = datetime.now()
        target = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            cursor = tasks_col.find({"status": STATUS_PENDING}, {"_id": 0})
            pending = await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or []
            today_date = datetime.now().date()
            for t in pending:
                due = parse_due(t.get("due", ""))
                if not due:
                    continue
                if due.date() < today_date:
                    updates = {"missed_flagged": True}
                    if not t.get("missed_counted"):
                        updates["missed_counted"] = True
                        state = await get_user_state(t["uid"])
                        await save_user_state(t["uid"], {"total_missed": state.get("total_missed", 0) + 1})
                    await update_task(t["id"], updates)
                    text = (
                        f"❌ *Не встигли зробити вчасно*\n\n"
                        f"\"{t.get('text','')}\"\n"
                        f"🕐 Було заплановано: {t.get('due','')}\n\n"
                        f"Перенести?"
                    )
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_rollover_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send rollover notice for task %s", t.get("id"))
        except Exception:
            logger.exception("midnight_rollover_task failed")

async def daily_job_task():
    while True:
        try:
            hh, mm = map(int, DAILY_REPORT_TIME.split(":"))
        except ValueError:
            hh, mm = 21, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            for uid in uids:
                try:
                    stats = await compute_daily_stats(uid)
                    streak = await update_streak(uid, stats["missed_count"])
                    await bot.send_message(uid, build_daily_summary_text(stats, streak))

                    # Вечірній AI-аналіз дня (не ламає звичайний підсумок, якщо AI недоступний)
                    if openai_client:
                        try:
                            analysis = await generate_ai_analysis(uid)
                            if analysis:
                                await bot.send_message(uid, f"🌙 *AI Підсумок дня*\n\n{analysis}")
                        except Exception:
                            logger.exception("Вечірній AI-аналіз не вдався для uid %s", uid)

                    cursor = tasks_col.find({"uid": uid, "postponed_today": True}, {"_id": 0, "id": 1})
                    postponed_ids = [d["id"] for d in (await db_call(cursor.to_list(length=None), default=[], raise_on_fail=False) or [])]
                    for tid in postponed_ids:
                        await update_task(tid, {"postponed_today": False})

                    if target.day == 1:
                        state = await get_user_state(uid)
                        month_key = target.strftime("%Y-%m")
                        if state.get("archive_prompt_month") != month_key:
                            done_count = len(await get_user_tasks(uid, statuses=[STATUS_DONE]))
                            if done_count > 0:
                                await bot.send_message(
                                    uid,
                                    f"У вас\n\n{done_count} виконаних задач.\n\nОчистити архів?",
                                    reply_markup=ikb_archive_clear()
                                )
                            await save_user_state(uid, {"archive_prompt_month": month_key})
                except Exception:
                    logger.exception("daily_job_task failed for uid %s", uid)
        except Exception:
            logger.exception("daily_job_task outer loop failed")

async def ai_morning_plan_task():
    """Раз на день о AI_DAILY_PLAN_TIME генерує AI-план для кожного авторизованого
    користувача (якщо AI увімкнений глобально й особисто для нього) і надсилає
    пропозицію з кнопками. Задачі в MongoDB НЕ створюються без підтвердження.
    last_ai_plan_date позначається ДО генерації, щоб рестарт бота не спричинив дубль."""
    if not AI_DAILY_PLAN_ENABLED or not openai_client:
        logger.info("Автоматичний AI ранковий план вимкнено (немає ключа або AI_DAILY_PLAN_ENABLED=false)")
        return
    while True:
        try:
            hh, mm = map(int, AI_DAILY_PLAN_TIME.split(":"))
        except ValueError:
            hh, mm = 9, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            uids = await get_all_uids()
            today_str = datetime.now().strftime("%Y-%m-%d")
            for uid in uids:
                try:
                    state = await get_user_state(uid)
                    if not state.get("ai_morning_enabled", True):
                        continue
                    if state.get("last_ai_plan_date") == today_str:
                        continue
                    # Позначаємо ДО генерації — захист від повторної відправки після рестарту
                    await save_user_state(uid, {"last_ai_plan_date": today_str})

                    plan = await generate_ai_plan(uid)
                    if not plan or not plan.get("tasks"):
                        continue
                    selected = set(range(len(plan["tasks"])))
                    ai_suggestions_cache[uid] = {"plan": plan, "selected": selected}
                    intro = "☀️ *Доброго ранку!*\n\n" + fmt_ai_plan_preview(plan, selected)
                    await bot.send_message(
                        uid, intro,
                        reply_markup=ikb_ai_plan_preview(plan["tasks"], selected),
                    )
                except Exception:
                    logger.exception("ai_morning_plan_task failed for uid %s", uid)
        except Exception:
            logger.exception("ai_morning_plan_task outer loop failed")

from aiohttp import web

async def ping(request):
    return web.Response(status=204)

async def main():
    init_mongo()
    try:
        await mongo_client.admin.command("ping")
        logger.info("MongoDB connection OK")
    except Exception:
        logger.exception("MongoDB connection FAILED at startup")

    await load_authorized_uids()

    app = web.Application()
    app.router.add_get("/", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

    asyncio.create_task(reminder_task())
    asyncio.create_task(midnight_rollover_task())
    asyncio.create_task(daily_job_task())
    asyncio.create_task(ai_morning_plan_task())

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот завдань запущено (MongoDB)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())