import json
import logging
import re

from openai import AsyncOpenAI

from config.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL

logger = logging.getLogger("tasks_bot")

client: AsyncOpenAI | None = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None

if not client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")

_model_verified = False

# Деякі AI-провайдери (гейтвеї з вбудованою модерацією) дописують у кінець
# (або й замість) відповіді службові рядки на кшталт:
#   User Safety: safe
#   Response Safety: safe
# Це не частина реальної відповіді моделі — прибираємо їх, інакше вони
# протікають користувачу як "англійський сміттєвий текст" і ламають
# json.loads() у generate_json().
_SAFETY_LINE_RE = re.compile(
    r"^\s*(user|response|input|output|prompt)\s*safety\s*:\s*\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_safety_noise(text: str) -> str:
    if not text:
        return text
    cleaned = _SAFETY_LINE_RE.sub("", text)
    # прибираємо зайві порожні рядки, що лишились після видалення міток
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


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


def _extract_json_object(raw: str) -> str | None:
    """
    Витягує перший повний JSON-об'єкт { ... } з тексту, ігноруючи будь-який
    сміттєвий текст до чи після нього (напр. safety-мітки провайдера).
    Рахує баланс дужок, враховуючи рядкові літерали, щоб не збитись
    на '{' / '}' всередині рядків JSON.
    """
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


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
        raw = (resp.choices[0].message.content or "").strip()
        return _strip_safety_noise(raw)
    except Exception:
        logger.exception("AI request failed (model=%s)", AI_MODEL)
        return None


async def generate_text(prompt: str, temperature: float = 0.6) -> str | None:
    return await _complete(prompt, temperature, json_mode=False)


async def generate_json(prompt: str, temperature: float = 0.7) -> dict | None:
    raw = await _complete(prompt, temperature, json_mode=True)
    if raw is None:
        return None
    candidate = _extract_json_object(raw) or _strip_json_fence(raw)
    try:
        data = json.loads(candidate)
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
        raw = (resp.choices[0].message.content or "").strip()
        return _strip_safety_noise(raw)
    except Exception:
        logger.exception("AI chat request failed (model=%s)", AI_MODEL)
        return None