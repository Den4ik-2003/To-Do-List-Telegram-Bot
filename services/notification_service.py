import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from database import users as users_db

logger = logging.getLogger("tasks_bot")


async def safe_send(bot: Bot, uid: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError:
        logger.exception("Не вдалося надіслати повідомлення uid=%s", uid)
        return False


async def should_send_morning_plan(uid: int, today_str: str) -> bool:
    state = await users_db.get_user_state(uid)
    if not state.get("ai_morning_enabled", True):
        return False
    return state.get("last_ai_plan_date") != today_str


async def mark_morning_plan_sent(uid: int, today_str: str):
    await users_db.save_user_state(uid, {"last_ai_plan_date": today_str})


async def should_send_evening_analysis(uid: int) -> bool:
    state = await users_db.get_user_state(uid)
    return state.get("ai_evening_enabled", True)


def build_daily_summary_text(stats: dict, streak: int) -> str:
    lines = [
        f"📅 *Підсумок дня — {stats['date_str']}*", "",
        f"✅ Виконано\n{stats['done_count']} задач", "",
        f"❌ Не виконано\n{stats['missed_count']}", "",
    ]
    if stats.get("longest"):
        seconds, text = stats["longest"]
        total_min = int(seconds // 60)
        h, m = divmod(total_min, 60)
        duration = f"{h} год {m} хв" if h and m else (f"{h} год" if h else f"{m} хв")
        lines.append(f"⏱ Найдовша задача\n{duration} ({text[:30]})")
        lines.append("")
    lines.append(f"🔥 Серія\n{streak} днів")
    if stats.get("postponed_count"):
        lines.append("")
        lines.append(f"↪️ Перенесено на пізніше\n{stats['postponed_count']} задач")
    return "\n".join(lines)


def build_weekly_summary_text(stats: dict) -> str:
    lines = [
        "📊 *Підсумок тижня*", "",
        f"✅ Виконано задач: *{stats['done_count']}*",
        f"❌ Пропущено задач: *{stats['missed_count']}*",
        f"📈 Дохід: *{stats['income']} грн*",
        f"📉 Витрати: *{stats['expense']} грн*",
        f"💵 Чистий результат: *{stats['net']} грн*",
        f"📁 Активних проєктів: *{stats['active_projects_count']}*",
    ]
    return "\n".join(lines)