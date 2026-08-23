import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config.constants import AI_ERROR_TEXT, AI_LIMIT_TEXT
from config.settings import AI_DAILY_LIMIT
from database import ai_usage as ai_usage_db
from services import ai_service
from handlers.common import require_auth
from handlers.ai_chat import handle_text_message

logger = logging.getLogger("tasks_bot")
router = Router(name="voice")


@router.message(F.voice)
async def voice_message(msg: Message, state: FSMContext, bot):
    if not await require_auth(msg, state):
        return
    if not ai_service.is_available():
        return await msg.answer(AI_ERROR_TEXT)
    if not ai_service.voice_available():
        return await msg.answer(
            "⚠️ Розпізнавання голосу не налаштоване. Потрібен WHISPER_API_KEY у змінних середовища."
        )

    uid = msg.from_user.id
    remaining = await ai_usage_db.get_remaining(uid, AI_DAILY_LIMIT)
    if remaining <= 0:
        return await msg.answer(AI_LIMIT_TEXT)

    wait_msg = await msg.answer("🎙 Розпізнаю голосове...")

    try:
        file = await bot.get_file(msg.voice.file_id)
        buf = await bot.download_file(file.file_path)
        audio_bytes = buf.read()
    except Exception:
        logger.exception("Не вдалося завантажити голосове для uid=%s", uid)
        return await wait_msg.edit_text("⚠️ Не вдалося завантажити голосове. Спробуй ще раз.")

    text = await ai_service.transcribe_voice(audio_bytes)
    if not text:
        return await wait_msg.edit_text(
            "🤔 Не вдалося розпізнати мову. Спробуй ще раз або напиши текстом у «💬 AI Чат»."
        )

    await wait_msg.edit_text(f"🎙 Розпізнано: «{text}»\n\n🤖 Обробляю...")
    await handle_text_message(uid, text, wait_msg)