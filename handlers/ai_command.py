import json
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config.constants import (
    AI_ERROR_TEXT, AI_LIMIT_TEXT, DEFAULT_CURRENCY,
    STATUS_PENDING,
    TRANSACTION_INCOME, TRANSACTION_EXPENSE,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
)
from config.settings import AI_DAILY_LIMIT
from database.mongo import DBUnavailable
from database import tasks as tasks_db
from database import finances as finances_db
from database import ai_usage as ai_usage_db
from services import ai_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="ai_command")


class AICommand(StatesGroup):
    waiting = State()


# ---------------------------------------------------------------------------
# Мінімальний набір функцій саме для швидких одноразових команд:
# "купив продукти на 500" → витрата, "купити молоко" → задача.
# tool_choice="required" — модель ЗОБОВ'ЯЗАНА викликати одну з функцій,
# бо тут нема сценарію "просто відповісти текстом" як у звичайному чаті.
# ---------------------------------------------------------------------------
AI_COMMAND_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Додає нову задачу/нагадування/подію. Викликай, коли фраза означає щось "
                "ЗРОБИТИ в майбутньому: 'купити X', 'написати X', 'зателефонувати X', "
                "'зустріч з X', 'нагадай X' тощо — тобто дію, яку ще не виконано."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Короткий опис задачі, напр. 'Купити молоко' або 'Зустріч з Андрієм'.",
                    },
                    "due": {
                        "type": "string",
                        "description": (
                            "Дата і час у форматі YYYY-MM-DDTHH:MM, якщо вказано в фразі "
                            "('завтра о 10', 'через 2 години'). Порахуй відносно сьогоднішньої "
                            "дати з системного повідомлення. Якщо не вказано — порожній рядок."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "enum": ["urgent", "medium", "low", "idea", "personal"],
                        "description": "Пріоритет. За замовчуванням 'medium'.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_transaction",
            "description": (
                "Додає фінансову операцію. Викликай, коли фраза означає ВЖЕ ЗДІЙСНЕНУ витрату "
                "або дохід: 'купив X на N грн', 'витратив N на X', 'отримав N за X', "
                "'заробив N' тощо — тобто дію, яка вже сталася і має суму грошей."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tx_type": {
                        "type": "string",
                        "enum": ["income", "expense"],
                        "description": "'expense' для витрат, 'income' для доходів.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Сума операції, позитивне число.",
                    },
                    "category": {
                        "type": "string",
                        "enum": list(EXPENSE_CATEGORIES.keys()) + list(INCOME_CATEGORIES.keys()),
                        "description": (
                            "Категорія. Для витрат: food (продукти/їжа), transport, home, health, "
                            "entertainment, shopping, project, other. Для доходів: salary, freelance, "
                            "project, gift, other. 'товари', 'продукти', 'їжа' → food."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Короткий опис, напр. 'продукти на тиждень'.",
                    },
                },
                "required": ["tx_type", "amount", "category"],
            },
        },
    },
]


@router.message(F.text == "🗣 AI Команди")
async def ai_command_start(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT, reply_markup=kb_main())

    await state.set_state(AICommand.waiting)
    await msg.answer(
        "🗣 *AI Команди*\n\n"
        "Напиши одним повідомленням, що зробити — я одразу виконаю:\n\n"
        "• «Купив продукти на 500 грн» → додасть витрату\n"
        "• «Купити молоко» → додасть задачу\n"
        "• «Завтра о 10 зустріч з Андрієм» → додасть задачу з датою\n"
        "• «Отримав 3000 грн за фріланс» → додасть дохід\n\n"
        "Щоб вийти — натисни «❌ Скасувати».",
        reply_markup=kb_cancel(),
    )


@router.message(AICommand.waiting)
async def ai_command_message(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("🗣 AI Команди завершено.", reply_markup=kb_main())

    uid = msg.from_user.id
    text = msg.text or ""
    if not text.strip():
        return

    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT)

    processing = await msg.answer("🤖 Обробляю...")

    try:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        system_text = (
            f"Сьогоднішня дата: {today_str}, поточний час: {now.strftime('%H:%M')}.\n"
            "Користувач пише одну команду. Визнач, чи це задача (щось зробити в майбутньому) "
            "чи фінансова операція (щось вже куплено/отримано з конкретною сумою), і виклич "
            "відповідну функцію. Завжди викликай рівно одну функцію."
        )
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": text},
        ]

        ai_message = await ai_service.chat_with_tools(messages, AI_COMMAND_TOOLS)
        if ai_message is None:
            return await processing.edit_text(AI_ERROR_TEXT)

        tool_calls = getattr(ai_message, "tool_calls", None)
        if not tool_calls:
            return await processing.edit_text(
                "🤔 Не зрозумів команду. Спробуй чіткіше, наприклад:\n"
                "«Купив продукти на 500 грн» або «Купити молоко»."
            )

        results = []
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await _execute_tool_call(uid, tc.function.name, args)
            results.append(result)

        await ai_usage_db.increment_usage(uid)
        await processing.edit_text("\n".join(results))
    except Exception:
        logger.exception("ai_command handling failed for uid=%s", uid)
        try:
            await processing.edit_text(AI_ERROR_TEXT)
        except Exception:
            pass


async def _execute_tool_call(uid: int, name: str, args: dict) -> str:
    if name == "add_task":
        title = (args.get("title") or "").strip()
        if not title:
            return "❌ Не вказано назву задачі, задачу не додано."
        try:
            tid = await tasks_db.next_task_id()
            task = {
                "id": tid,
                "uid": uid,
                "title": title,
                "due": (args.get("due") or "").strip(),
                "label": args.get("label") or "medium",
                "status": STATUS_PENDING,
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            await tasks_db.add_task(task)
            due_text = f" на {task['due']}" if task["due"] else ""
            return f"✅ Задачу додано: «{title}»{due_text}"
        except DBUnavailable:
            return "❌ База даних тимчасово недоступна, задачу не додано."
        except Exception:
            logger.exception("Не вдалося додати задачу через AI команду для uid=%s", uid)
            return "❌ Не вдалося додати задачу через технічну проблему."

    if name == "add_transaction":
        tx_type = args.get("tx_type")
        amount = args.get("amount")
        category = args.get("category") or "other"
        description = (args.get("description") or "").strip()

        if tx_type not in (TRANSACTION_INCOME, TRANSACTION_EXPENSE):
            return "❌ Некоректний тип операції, не додано."
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return "❌ Некоректна сума, операцію не додано."
        if amount <= 0:
            return "❌ Сума має бути більшою за нуль."

        try:
            await finances_db.add_transaction(
                uid=uid,
                tx_type=tx_type,
                amount=amount,
                category=category,
                description=description,
            )
            kind = "дохід" if tx_type == TRANSACTION_INCOME else "витрату"
            cat_label = EXPENSE_CATEGORIES.get(category) or INCOME_CATEGORIES.get(category) or {}
            cat_name = cat_label.get("name", category)
            return f"✅ Додано {kind}: {amount:.0f} {DEFAULT_CURRENCY} ({cat_name})"
        except DBUnavailable:
            return "❌ База даних тимчасово недоступна, операцію не додано."
        except Exception:
            logger.exception("Не вдалося додати транзакцію через AI команду для uid=%s", uid)
            return "❌ Не вдалося додати операцію через технічну проблему."

    return f"❌ Невідома функція: {name}"