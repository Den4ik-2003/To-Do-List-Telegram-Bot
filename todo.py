import asyncio
import csv
import io
import logging
import os
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tasks_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
MONGO_URI = os.environ["MONGO_URI"]

REMINDER_BEFORE_MINUTES = int(os.environ.get("REMINDER_BEFORE_MINUTES", "10"))
OVERDUE_GRACE_MINUTES = int(os.environ.get("OVERDUE_GRACE_MINUTES", "15"))
DAILY_REPORT_TIME = os.environ.get("DAILY_REPORT_TIME", "21:00")

mongo_client: AsyncIOMotorClient | None = None
db = None
tasks_col = None
users_col = None
auth_col = None

authorized_uids: set[int] = set()

def init_mongo():
    global mongo_client, db, tasks_col, users_col, auth_col
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

PRIORITIES = ["high", "medium", "low"]
PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PRIORITY_LABEL = {"high": "Високий", "medium": "Середній", "low": "Низький"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

STATUS_PENDING = "pending"
STATUS_OVERDUE = "overdue"
STATUS_DONE = "done"

DB_ERROR_TEXT = "⚠️ Тимчасова проблема з базою даних. Спробуйте ще раз через кілька секунд."

MONTHS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

async def db_call(coro, default=None, retries=2):
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
    docs = await db_call(cursor.to_list(length=None), default=[]) or []
    for d in docs:
        if "uid" in d:
            authorized_uids.add(d["uid"])
    logger.info("Loaded %d authorized users into cache", len(authorized_uids))

async def get_all_uids() -> list:
    if authorized_uids:
        return list(authorized_uids)
    cursor = auth_col.find({}, {"_id": 0, "uid": 1})
    docs = await db_call(cursor.to_list(length=None), default=[]) or []
    return [d["uid"] for d in docs if "uid" in d]

async def get_user_state(uid: int) -> dict:
    doc = await db_call(users_col.find_one({"uid": uid}))
    if not doc:
        return {"uid": uid, "streak": 0, "last_streak_date": "", "archive_prompt_month": ""}
    return doc

async def save_user_state(uid: int, fields: dict):
    await db_call(users_col.update_one({"uid": uid}, {"$set": fields}, upsert=True))

async def next_task_id() -> int:
    doc = await db_call(tasks_col.find_one(sort=[("id", -1)]))
    return (doc["id"] + 1) if doc else 1

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
        return (t.get("due", ""), PRIORITY_ORDER.get(t.get("priority", "low"), 2))
    return sorted(tasks, key=key)

def sort_tasks_by_priority_then_due(tasks: list) -> list:
    def key(t):
        return (PRIORITY_ORDER.get(t.get("priority", "low"), 2), t.get("due", ""))
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

def fmt_task(t: dict, short: bool = False) -> str:
    pe = PRIORITY_EMOJI.get(t.get("priority", "low"), "")
    status = t.get("status", STATUS_PENDING)
    status_icon = {"pending": "⏳", "overdue": "❌", "done": "✅"}.get(status, "⏳")
    lines = [
        f"*№{t['id']}* {pe} {status_icon}",
        f"📝 {t.get('text','')}",
        f"🕐 {t.get('due','')}",
        f"⚡ Пріоритет: *{PRIORITY_LABEL.get(t.get('priority','low'),'—')}*",
    ]
    if not short:
        if t.get("postponed_count"):
            lines.append(f"🔁 Перенесено разів: *{t['postponed_count']}*")
        if status == STATUS_DONE and t.get("completed_at"):
            lines.append(f"✅ Виконано: {t['completed_at'][:16].replace('T',' ')}")
    return "\n".join(lines)

def build_task_list_text(tasks: list, title: str) -> str:
    if not tasks:
        return f"{title}\n\n📭 Немає завдань."
    lines = [title, ""]
    for t in tasks:
        pe = PRIORITY_EMOJI.get(t.get("priority", "low"), "")
        status_icon = {"pending": "⏳", "overdue": "❌", "done": "✅"}.get(t.get("status"), "⏳")
        lines.append(f"{status_icon} {pe} №{t['id']} — {t.get('due','')[-5:]} {t.get('text','')[:40]}")
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
        elif t.get("missed_flagged"):
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

class Auth(StatesGroup):
    waiting_password = State()

class AddTask(StatesGroup):
    text = State()
    priority = State()
    date = State()
    date_manual = State()
    time = State()

class EditField(StatesGroup):
    typing = State()

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Нове завдання"), KeyboardButton(text="📋 Сьогодні")],
        [KeyboardButton(text="📆 Всі активні"), KeyboardButton(text="🔥 Серія")],
        [KeyboardButton(text="📦 Архів"), KeyboardButton(text="📖 Підсумок дня")],
        [KeyboardButton(text="📤 Експорт CSV")],
    ], resize_keyboard=True)

def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)

def kb_priority() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Високий"), KeyboardButton(text="🟡 Середній"), KeyboardButton(text="🟢 Низький")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def kb_date() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Сьогодні"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="✏️ Своя дата (дд.мм.рррр)")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)

def priority_from_text(text: str) -> str | None:
    mapping = {"🔴 Високий": "high", "🟡 Середній": "medium", "🟢 Низький": "low"}
    return mapping.get(text)

def ikb_task_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
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

def ikb_overdue_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Через 1 год", callback_data=f"postp1h:{tid}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}")],
        [InlineKeyboardButton(text="❌ Видалити", callback_data=f"deltask:{tid}")],
    ])

def ikb_reminder_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
    ])

def ikb_edit_fields(tid: int) -> InlineKeyboardMarkup:
    fields = [("text", "📝 Текст"), ("priority", "⚡ Пріоритет"), ("date", "📅 Дата"), ("time", "🕐 Час")]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"editfield:{tid}:{key}")] for key, label in fields]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_tasks_list(tasks: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = tasks[start:start + per_page]
    rows = []
    for t in chunk:
        pe = PRIORITY_EMOJI.get(t.get("priority", "low"), "")
        status_icon = {"pending": "⏳", "overdue": "❌", "done": "✅"}.get(t.get("status"), "⏳")
        label = f"{status_icon} {pe} №{t['id']} {t.get('due','')[-5:]} {t.get('text','')[:20]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"view:{t['id']}")])
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

def ikb_archive_clear() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Так", callback_data="archclr:yes"),
            InlineKeyboardButton(text="❌ Ні", callback_data="archclr:no"),
        ],
    ])

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher(storage=MemoryStorage())

user_list_cache: dict = {}

async def require_auth(msg: Message, state: FSMContext) -> bool:
    if await is_authorized(msg.from_user.id):
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
    return True

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if await is_authorized(msg.from_user.id):
        await msg.answer("👋 *Менеджер завдань*\n\nОбери дію:", reply_markup=kb_main())
    else:
        await state.set_state(Auth.waiting_password)
        await msg.answer("🔒 *Доступ закритий*\n\nВведіть пароль:", reply_markup=ReplyKeyboardRemove())

@dp.message(Auth.waiting_password)
async def check_password(msg: Message, state: FSMContext):
    if msg.text == BOT_PASSWORD:
        await authorize(msg.from_user.id)
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
    await state.set_state(AddTask.priority)
    await msg.answer("⚡ Оберіть *пріоритет*:", reply_markup=kb_priority())

@dp.message(AddTask.priority)
async def at_priority(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear(); return await msg.answer("Скасовано.", reply_markup=kb_main())
    priority = priority_from_text(msg.text)
    if not priority:
        return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_priority())
    await state.update_data(priority=priority)
    await state.set_state(AddTask.date)
    await msg.answer("📅 Коли виконати? Оберіть дату:", reply_markup=kb_date())

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
    task = {
        "id": await next_task_id(),
        "uid": msg.from_user.id,
        "text": fd["text"],
        "priority": fd["priority"],
        "due": fmt_due(due_dt),
        "status": STATUS_PENDING,
        "created_at": created_at,
        "completed_at": None,
        "reminded_before": False,
        "overdue_notified": False,
        "missed_flagged": False,
        "postponed_count": 0,
        "postponed_today": False,
    }
    if due_dt <= datetime.now():
        task["status"] = STATUS_OVERDUE
        task["overdue_notified"] = True
        task["missed_flagged"] = True

    await add_task(task)
    await msg.answer(f"✅ *Завдання додано!*\n\n{fmt_task(task)}", reply_markup=kb_main())

@dp.message(F.text == "📋 Сьогодні")
async def today_tasks(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    try:
        uid = msg.from_user.id
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_OVERDUE, STATUS_DONE])
        tasks = [t for t in tasks if is_today(t.get("due", ""))]
        tasks = sort_tasks_by_priority_then_due(tasks)
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
        tasks = await get_user_tasks(uid, statuses=[STATUS_PENDING, STATUS_OVERDUE])
        tasks = sort_tasks(tasks)
        user_list_cache[uid] = tasks
        if not tasks:
            return await msg.answer("📭 Активних завдань немає.", reply_markup=kb_main())
        await msg.answer(f"📆 *Всі активні* — {len(tasks)} шт.", reply_markup=kb_main())
        await msg.answer("Обери завдання:", reply_markup=ikb_tasks_list(tasks))
    except Exception:
        logger.exception("active_tasks failed")
        await msg.answer(DB_ERROR_TEXT, reply_markup=kb_main())

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
        kb = ikb_overdue_actions(tid) if t.get("status") == STATUS_OVERDUE else ikb_task_actions(tid)
        await cb.message.edit_text(fmt_task(t), reply_markup=kb)
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
        await update_task(tid, {"status": STATUS_DONE, "completed_at": datetime.now().isoformat()})
        t = await get_task(tid)
        try:
            await cb.message.edit_text(f"✅ *Виконано!*\n\n{fmt_task(t)}")
        except TelegramAPIError:
            await cb.message.answer(f"✅ *Виконано!*\n\n{fmt_task(t)}")
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
        "overdue_notified": False,
        "postponed_count": t.get("postponed_count", 0) + 1,
        "postponed_today": True,
    })
    t = await get_task(tid)
    try:
        await cb.message.edit_text(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid))
    except TelegramAPIError:
        await cb.message.answer(f"🔁 *Перенесено!*\n\n{fmt_task(t)}", reply_markup=ikb_task_actions(tid))
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
            "priority": "Пріоритет (🔴 Високий / 🟡 Середній / 🟢 Низький)",
            "date": "Дата (дд.мм.рррр)",
            "time": "Час (гг:хх)",
        }
        await state.set_state(EditField.typing)
        await state.update_data(edit_tid=tid, edit_field=field)
        kb = kb_priority() if field == "priority" else kb_cancel()
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
    elif field == "priority":
        priority = priority_from_text(msg.text)
        if not priority:
            return await msg.answer("⚠️ Оберіть один із варіантів на клавіатурі:", reply_markup=kb_priority())
        await update_task(tid, {"priority": priority})
    elif field == "date":
        try:
            datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *дд.мм.рррр*:", reply_markup=kb_cancel())
        old_due = parse_due(t.get("due", ""))
        time_part = old_due.strftime("%H:%M") if old_due else "00:00"
        new_due = f"{msg.text.strip()} {time_part}"
        await update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                 "reminded_before": False, "overdue_notified": False})
    elif field == "time":
        try:
            datetime.strptime(msg.text.strip(), "%H:%M")
        except ValueError:
            return await msg.answer("⚠️ Невірний формат. Введіть як *гг:хх*:", reply_markup=kb_cancel())
        old_due = parse_due(t.get("due", ""))
        date_part = old_due.strftime("%d.%m.%Y") if old_due else datetime.now().strftime("%d.%m.%Y")
        new_due = f"{date_part} {msg.text.strip()}"
        await update_task(tid, {"due": new_due, "status": STATUS_PENDING,
                                 "reminded_before": False, "overdue_notified": False})

    await state.clear()
    t = await get_task(tid)
    if t:
        await msg.answer(f"✅ Оновлено!\n\n{fmt_task(t)}", reply_markup=kb_main())
    else:
        await msg.answer("Завдання не знайдено.", reply_markup=kb_main())

@dp.message(F.text == "🔥 Серія")
async def streak_view(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    streak = await get_streak(msg.from_user.id)
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
    tasks = await get_user_tasks(uid, statuses=[STATUS_DONE])
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
    stats = await compute_daily_stats(msg.from_user.id)
    streak = await get_streak(msg.from_user.id)
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

@dp.message(F.text == "📤 Експорт CSV")
async def export_csv(msg: Message, state: FSMContext):
    if not await require_auth(msg, state): return
    uid = msg.from_user.id
    tasks = sort_tasks(await get_user_tasks(uid))
    if not tasks:
        return await msg.answer("📭 Немає завдань для експорту.", reply_markup=kb_main())
    output = io.StringIO()
    fieldnames = ["id", "text", "priority", "due", "status", "completed_at", "postponed_count"]
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
            now = datetime.now()
            cursor = tasks_col.find({"status": STATUS_PENDING}, {"_id": 0})
            pending = await db_call(cursor.to_list(length=None), default=[]) or []
            pending = sort_tasks_by_priority_then_due(pending)

            for t in pending:
                due = parse_due(t.get("due", ""))
                if not due:
                    continue

                if not t.get("reminded_before") and due - now <= timedelta(minutes=REMINDER_BEFORE_MINUTES) and due > now:
                    text = (
                        f"⏰ *Нагадування!*\n\n"
                        f"Через {REMINDER_BEFORE_MINUTES} хв: *{t.get('text','')}*\n"
                        f"{PRIORITY_EMOJI.get(t.get('priority','low'),'')} {PRIORITY_LABEL.get(t.get('priority','low'),'')}\n"
                        f"🕐 {t.get('due','')}"
                    )
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_reminder_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send pre-reminder for task %s", t.get("id"))
                    await update_task(t["id"], {"reminded_before": True})

                if not t.get("overdue_notified") and now - due >= timedelta(minutes=OVERDUE_GRACE_MINUTES):
                    await update_task(t["id"], {
                        "status": STATUS_OVERDUE,
                        "overdue_notified": True,
                        "missed_flagged": True,
                    })
                    text = f"❌ *Ви не виконали*\n\n\"{t.get('text','')}\"\n\nПеренести?"
                    try:
                        await bot.send_message(t["uid"], text, reply_markup=ikb_overdue_actions(t["id"]))
                    except Exception:
                        logger.exception("Failed to send overdue notice for task %s", t.get("id"))
        except Exception:
            logger.exception("reminder_task loop failed")

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

                    cursor = tasks_col.find({"uid": uid, "postponed_today": True}, {"_id": 0, "id": 1})
                    postponed_ids = [d["id"] for d in (await db_call(cursor.to_list(length=None), default=[]) or [])]
                    for tid in postponed_ids:
                        await update_task(tid, {"postponed_today": False})

                    if now.day == 1:
                        state = await get_user_state(uid)
                        month_key = now.strftime("%Y-%m")
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

from aiohttp import web

async def health(request):
    try:
        await mongo_client.admin.command("ping")
        return web.Response(text="OK")
    except Exception:
        return web.Response(text="DB_DOWN", status=503)

async def main():
    init_mongo()
    try:
        await mongo_client.admin.command("ping")
        logger.info("MongoDB connection OK")
    except Exception:
        logger.exception("MongoDB connection FAILED at startup")

    await load_authorized_uids()

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

    asyncio.create_task(reminder_task())
    asyncio.create_task(daily_job_task())

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот завдань запущено (MongoDB)...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())