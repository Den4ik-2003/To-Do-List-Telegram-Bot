import json
import logging
import re
import base64

from openai import AsyncOpenAI

from config.settings import AI_API_KEY, AI_BASE_URL, AI_MODEL, WHISPER_API_KEY, WHISPER_BASE_URL, WHISPER_MODEL

logger = logging.getLogger("tasks_bot")

client: AsyncOpenAI | None = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None
whisper_client: AsyncOpenAI | None = AsyncOpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_BASE_URL) if WHISPER_API_KEY else None

if not client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")
if not whisper_client:
    logger.warning("WHISPER_API_KEY не задано — розпізнавання голосових повідомлень вимкнено")

_model_verified = False

_SAFETY_LINE_RE = re.compile(
    r"^\s*(user|response|input|output|prompt)\s*safety\s*:\s*\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_safety_noise(text: str) -> str:
    if not text:
        return text
    cleaned = _SAFETY_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def is_available() -> bool:
    return client is not None


def voice_available() -> bool:
    return whisper_client is not None


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


async def chat_with_tools(messages: list[dict], tools: list[dict], temperature: float = 0.7):
    if not client:
        return None
    try:
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=temperature,
        )
        return resp.choices[0].message
    except Exception:
        logger.warning("Модель %s не прийняла tools, повторюю без них", AI_MODEL, exc_info=True)
        try:
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message
        except Exception:
            logger.exception("AI chat_with_tools request failed (model=%s)", AI_MODEL)
            return None


async def extract_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict | None:
    if not client:
        return None

    b64 = base64.b64encode(image_bytes).decode()
    prompt_text = (
        "Це фото чека з магазину (українською або польською). "
        "Витягни дані та поверни ЛИШЕ JSON без пояснень:\n"
        '{"total": число, "currency": "UAH або PLN", '
        '"category": одне з ["food","transport","home","health","entertainment","shopping","project","other"], '
        '"items": ["назва товару", ...]}\n'
        "Якщо валюта не вказана явно — визнач з мови/контексту або постав UAH. "
        "Якщо не можеш розпізнати суму — постав total: 0."
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ],
    }]

    try:
        try:
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
        except Exception:
            logger.warning("Модель %s без response_format для vision, повторюю без нього", AI_MODEL)
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                temperature=0.2,
            )
        raw = _strip_safety_noise((resp.choices[0].message.content or "").strip())
    except Exception:
        logger.exception("AI extract_receipt request failed (model=%s)", AI_MODEL)
        return None

    candidate = _extract_json_object(raw) or _strip_json_fence(raw)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        logger.exception("AI повернув некоректний JSON для чека: %s", raw[:300])
        return None
    if not isinstance(data, dict):
        return None
    return data


async def transcribe_voice(audio_bytes: bytes) -> str | None:
    if not whisper_client:
        return None
    try:
        resp = await whisper_client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=("voice.ogg", audio_bytes),
            language="uk",
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception:
        logger.exception("Voice transcription failed")
        return None


async def analyze_product_photo(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict | None:
    """
    Аналізує фото товару і повертає:
    {"title": str, "description": str, "category": str, "price_uah": float, "price_reasoning": str}

    ВАЖЛИВО: назва, опис і категорія генеруються ВИКЛЮЧНО українською мовою,
    ціна оцінюється в гривнях (орієнтир — український ринок вживаних товарів,
    напр. OLX.ua), а не в злотих/польською, як було раніше.
    """
    if not client:
        return None

    b64 = base64.b64encode(image_bytes).decode()
    prompt_text = (
        "Це фото товару, який людина хоче продати на OLX (Україна). "
        "Визнач, що це за товар, і поверни ЛИШЕ JSON без пояснень. "
        "Усі текстові поля (title, description, category, price_reasoning) "
        "пиши ВИКЛЮЧНО УКРАЇНСЬКОЮ МОВОЮ, навіть якщо на фото є іноземні написи чи бренди:\n"
        '{"title": "коротка приваблива назва оголошення українською, до 60 символів", '
        '"description": "опис товару українською, 2-4 речення: стан, особливості, чому варто купити", '
        '"category": "категорія товару українською, напр. Електроніка/Меблі/Одяг/Спорт/Інше", '
        '"price_uah": число (приблизна ринкова ціна в гривнях, реалістична для вживаного товару '
        'такого типу на українському ринку), '
        '"price_reasoning": "одне речення українською чому саме така ціна"}\n'
        "Якщо не можеш точно визначити товар — вкажи найбільш ймовірний варіант і зазнач це в description. "
        "Оцінюй ціну консервативно, як для вживаного товару середнього стану, якщо стан не видно чітко з фото."
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ],
    }]

    try:
        try:
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                temperature=0.4,
                response_format={"type": "json_object"},
            )
        except Exception:
            logger.warning("Модель %s без response_format для фото товару, повторюю без нього", AI_MODEL)
            resp = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                temperature=0.4,
            )
        raw = _strip_safety_noise((resp.choices[0].message.content or "").strip())
    except Exception:
        logger.exception("AI analyze_product_photo request failed (model=%s)", AI_MODEL)
        return None

    candidate = _extract_json_object(raw) or _strip_json_fence(raw)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        logger.exception("AI повернув некоректний JSON для товару: %s", raw[:300])
        return None
    if not isinstance(data, dict):
        return None
    return data