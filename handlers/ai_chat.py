import json
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT, AI_ERROR_TEXT, AI_LIMIT_TEXT, DEFAULT_CURRENCY, STATUS_PENDING, STATUS_DONE
from config.settings import AI_DAILY_LIMIT
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import goals as goals_db
from database import projects as projects_db
from database import finances as finances_db
from database import ai_usage as ai_usage_db
from database import conversations as conversations_db
from services import ai_service
from services import currency_service
from services import countdown_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.ai import ikb_chat_context_actions
from handlers.common import require_auth, is_missed

logger = logging.getLogger("tasks_bot")
router = Router(name="ai_chat")


class AIChat(StatesGroup):
    chatting = State()


QUICK_QUESTIONS = {
    "week": "Проаналізуй мій тиждень: що я встиг, а що ні, і дай пораду.",
    "goal_progress": "Чи я відстаю від своїх цілей? Наскільки?",
    "spend_advice": "Чи варто мені зараз витрачати гроші, зважаючи на мій баланс і бюджети?",
}

# ---------------------------------------------------------------------------
# Опис функцій, які AI може викликати під час чату.
# Модель сама вирішує, чи треба викликати функцію, виходячи з фрази
# користувача (напр. "додай мені таску купити молоко на завтра").
# ---------------------------------------------------------------------------
AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Додає нову задачу (to-do) користувачу в його список задач у базі даних. "
                "Викликай цю функцію, коли користувач явно просить щось додати, записати, "
                "нагадати зробити, внести в задачі тощо."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Короткий, зрозумілий опис задачі, напр. 'Купити молоко'.",
                    },
                    "due": {
                        "type": "string",
                        "description": (
                            "Дедлайн у форматі YYYY-MM-DD, якщо користувач його вказав "
                            "(в т.ч. 'завтра', 'у п'ятницю' — перерахуй у конкретну дату сам). "
                            "Якщо дедлайну немає — залиш порожній рядок."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "enum": ["work", "personal", "idea"],
                        "description": "Категорія задачі, якщо очевидна з контексту. Якщо незрозуміло — 'idea'.",
                    },
                },
                "required": ["title"],
            },
        },
    },
]


@router.message(F.text == "💬 AI Чат")
async def ai_chat_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())

    remaining = await ai_usage_db.get_remaining(msg.from_user.id, AI_DAILY_LIMIT)
    await state.set_state(AIChat.chatting)
    await msg.answer(
        "💬 *AI Чат*\n\n"
        "Постав будь-яке питання про свої задачі, цілі, проєкти чи фінанси — "
        "я відповім, спираючись на твої реальні дані.\n\n"
        "Також можеш попросити мене додати задачу прямо тут, напр.: "
        "«додай таску купити молоко на завтра».\n\n"
        "А ще можна просто написати конвертацію валют, напр.: `1500 PLN → UAH`, "
        "або запитати «скільки днів до 1 січня» — відповім миттєво, без AI.\n\n"
        f"🤖 Залишилось запитів сьогодні: *{remaining}*\n\n"
        "Щоб завершити — натисни «❌ Скасувати» або кнопку нижче.",
        reply_markup=kb_cancel(),
    )
    await msg.answer("Або обери швидке питання:", reply_markup=ikb_chat_context_actions())


@router.callback_query(F.data == "chat_close")
async def chat_close_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("💬 Чат завершено.")
    except Exception:
        pass
    await cb.message.answer("🏠 Головне меню:", reply_markup=kb_main())
    await cb.answer()


@router.callback_query(F.data.startswith("chat_quick:"))
async def chat_quick_cb(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    question = QUICK_QUESTIONS.get(key)
    if not question:
        return await cb.answer()
    await state.set_state(AIChat.chatting)
    await cb.answer()
    await cb.message.answer(f"❓ {question}")
    await _handle_chat_message(cb.from_user.id, question, cb.message)


@router.message(AIChat.chatting)
async def ai_chat_message(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("💬 Чат завершено.", reply_markup=kb_main())
    await _handle_chat_message(msg.from_user.id, msg.text or "", msg)


async def _build_context_text(uid: int) -> str:
    try:
        tasks = await tasks_db.get_user_tasks(uid)
        active = [t for t in tasks if t.get("status") == STATUS_PENDING]
        overdue = [t for t in active if is_missed(t)]
        today_key = datetime.now().strftime("%Y-%m-%d")
        done_today = [t for t in tasks if t.get("status") == STATUS_DONE and (t.get("completed_at") or "").startswith(today_key)]

        goals = await goals_db.get_active_goals(uid)
        projects = await projects_db.get_active_projects(uid)
        balance = await finances_db.get_balance(uid)

        now = datetime.now()
        month_start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")
        month_end = now.strftime("%Y-%m-%dT23:59:59")
        month = await finances_db.get_period_summary(uid, month_start, month_end)
    except DBUnavailable:
        return "(дані тимчасово недоступні через проблему з базою даних)"

    goals_text = "\n".join(
        f"- {g.get('title','')}" + (
            f" ({g.get('current_amount',0)}/{g.get('target_amount')} {DEFAULT_CURRENCY})"
            if g.get("goal_type") == "financial" and g.get("target_amount") else ""
        )
        for g in goals
    ) or "(немає активних цілей)"

    projects_text = "\n".join(
        f"- {p.get('title','')}" + (f" — бюджет {p.get('spent',0)}/{p.get('budget')} {DEFAULT_CURRENCY}" if p.get("budget") else "")
        for p in projects
    ) or "(немає активних проєктів)"

    today_str = datetime.now().strftime("%Y-%m-%d")

    return f"""Контекст користувача (реальні дані, не вигадуй нічого поверх цього):

Сьогоднішня дата: {today_str}

Активних задач: {len(active)}
Прострочених задач: {len(overdue)}
Виконано сьогодні: {len(done_today)}

Активні цілі:
{goals_text}

Активні проєкти:
{projects_text}

Баланс: {balance} {DEFAULT_CURRENCY}
Дохід цього місяця: {month['income']} {DEFAULT_CURRENCY}
Витрати цього місяця: {month['expense']} {DEFAULT_CURRENCY}

Якщо для відповіді на питання даних недостатньо — чесно скажи:
"У мене поки недостатньо даних для точної рекомендації."
Якщо користувач просить додати задачу — виклич функцію add_task з потрібними параметрами.
Відповідай українською, коротко і по суті."""


async def _execute_tool_call(uid: int, name: str, args: dict) -> str:
    """Виконує реальну дію в БД і повертає короткий текстовий результат — цей текст
    піде назад у модель, щоб вона сформувала фінальну відповідь користувачу."""
    if name == "add_task":
        title = (args.get("title") or "").strip()
        if not title:
            return "Помилка: не вказано назву задачі, задачу не додано."
        try:
            tid = await tasks_db.next_task_id()
            task = {
                "id": tid,
                "uid": uid,
                "text": title,
                "due": (args.get("due") or "").strip(),
                "label": args.get("label") or "medium",
                "status": STATUS_PENDING,
                "reminded_before": False,
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            await tasks_db.add_task(task)
            due_text = f" на {task['due']}" if task["due"] else ""
            return f"Задачу успішно додано (id={tid}): «{title}»{due_text}."
        except DBUnavailable:
            return "Помилка: база даних тимчасово недоступна, задачу не додано."
        except Exception:
            logger.exception("Не вдалося додати задачу через AI tool call для uid=%s", uid)
            return "Помилка: не вдалося додати задачу через технічну проблему."
    return f"Невідома функція: {name}"


async def _handle_chat_message(uid: int, text: str, target: Message):
    if not text.strip():
        return

    # Конвертер валют перевіряємо ДО AI — це швидше і не витрачає денний ліміт
    converted = await currency_service.try_convert(text)
    if converted:
        return await target.answer(converted)

    # Дні до дати — теж перевіряємо без AI, якщо розпізналось однозначно
    countdown_answer = await countdown_service.try_answer(uid, text)
    if countdown_answer:
        return await target.answer(countdown_answer)

    if not ai_service.is_available():
        return await target.answer(AI_ERROR_TEXT)

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await target.answer(AI_LIMIT_TEXT)

    thinking = await target.answer("🤖 Думаю...")

    try:
        context_text = await _build_context_text(uid)
        convo = await conversations_db.get_conversation(uid)
        history = convo.get("messages", [])[-10:]

        messages = [{"role": "system", "content": context_text}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        ai_message = await ai_service.chat_with_tools(messages, AI_TOOLS)
        if ai_message is None:
            return await thinking.edit_text(AI_ERROR_TEXT)

        tool_calls = getattr(ai_message, "tool_calls", None)

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": ai_message.content or "",
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            })
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await _execute_tool_call(uid, tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            reply = await ai_service.chat(messages)
        else:
            reply = ai_message.content

        if not reply:
            return await thinking.edit_text(AI_ERROR_TEXT)

        await ai_usage_db.increment_usage(uid)
        await conversations_db.append_message(uid, "user", text)
        await conversations_db.append_message(uid, "assistant", reply)

        await thinking.edit_text(reply)
    except Exception:
        logger.exception("ai_chat message handling failed for uid=%s", uid)
        try:
            await thinking.edit_text(AI_ERROR_TEXT)
        except Exception:
            pass