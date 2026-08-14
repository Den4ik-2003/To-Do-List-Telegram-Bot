import json
import logging

from openai import AsyncOpenAI

from config.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL

logger = logging.getLogger("tasks_bot")

client: AsyncOpenAI | None = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None

if not client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")

_model_verified = False


def is_available() -> bool:
    return client is not None


async def verify_model() -> bool:
    global _model_verified
    if not client:
        return False
    if _model_verified:
        return True
    try:
        models = await client.models.list()
        slugs = {m.id for m in models.data}
        if AI_MODEL not in slugs:
            logger.warning("AI_MODEL '%s' не знайдено серед доступних моделей провайдера", AI_MODEL)
        else:
            logger.info("AI_MODEL '%s' підтверджено провайдером", AI_MODEL)
        _model_verified = True
        return AI_MODEL in slugs
    except Exception:
        logger.exception("Не вдалося перевірити список моделей AI-провайдера")
        return False


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```")
    return raw.strip()


async def _complete(prompt: str, temperature: float, json_mode: bool) -> str | None:
    if not client:
        return None
    try:
        try:
            kwargs = {"temperature": temperature}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
        except Exception:
            if not json_mode:
                raise
            logger.warning("Модель %s не підтримує response_format=json_object, повторюю без нього", AI_MODEL)
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("AI request failed (model=%s)", AI_MODEL)
        return None


async def generate_text(prompt: str, temperature: float = 0.6) -> str | None:
    return await _complete(prompt, temperature, json_mode=False)


async def generate_json(prompt: str, temperature: float = 0.7) -> dict | None:
    raw = await _complete(prompt, temperature, json_mode=True)
    if raw is None:
        return None
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        logger.exception("AI повернув некоректний JSON: %s", raw[:300])
        return None
    if not isinstance(data, dict):
        return None
    return data


async def chat(messages: list[dict], temperature: float = 0.7) -> str | None:
    if not client:
        return None
    try:
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("AI chat request failed (model=%s)", AI_MODEL)
        return None