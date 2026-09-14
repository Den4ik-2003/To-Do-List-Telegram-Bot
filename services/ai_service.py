"""
ЗМІНЕНИЙ ФАЙЛ: services/ai_service.py

КОРІНЬ ПРОБЛЕМИ "AI не зміг сформувати сайт із цього опису": генерація
повного лендінгу (HTML+CSS+JS одним запитом, label="generate") — важка
задача для безкоштовної моделі, і вона регулярно НЕ встигала вкластись у
той самий 30-секундний таймаут, що використовувався для ЛЕГКИХ запитів
(chat, розбір чека тощо). Додатково, поверх внутрішнього retry цього
файлу, у services/website_builder_service.py був доданий ЩЕ ОДИН,
зовнішній retry — разом це давало до 4 повних мережевих спроб по ~30с,
і кожна впадала в таймаут, а користувач чекав ~2.5 хв і однаково
отримував відмову.

ЗМІНИ:
1. AI_GENERATE_TIMEOUT_SECONDS = 60 — окремий, довший таймаут ЛИШЕ для
   label="generate" (генерація сайту). Інші лейбли (chat,
   extract_receipt, analyze_product_photo) і далі використовують
   AI_REQUEST_TIMEOUT_SECONDS=30 — там довші відповіді не потрібні, і
   немає сенсу змушувати користувача чекати довше.
2. AI_CLIENT_TIMEOUT_SECONDS: 40 → 90. Це таймаут самого HTTP-клієнта
   OpenAI SDK — він має бути БІЛЬШИМ за найбільший з наших власних
   asyncio.wait_for(...), інакше клієнт обірве з'єднання ще до того, як
   спрацює наш таймаут, і ми ніколи не побачимо шансу на довшу, але
   потенційно успішну відповідь.
3. Для label="generate" прибрано внутрішній retry (тепер завжди 1 спроба
   на модель, а не MAX_TRANSIENT_RETRIES+1=2) — натомість ця одна спроба
   має вдвічі більше часу (60с замість 30с). Це свідомий трейд-офф:
   краще одна спроба з реалістичним вікном часу, ніж дві короткі, жодна
   з яких не встигає. Найгірший сценарій для generate_json() тепер:
   60с (json_mode) + 60с (текстовий fallback) = ~120с на одну модель,
   замість попередніх ~180с (тут) + подвоєння зовні в
   website_builder_service.py.
4. Легкі лейбли (chat/extract_receipt/analyze_product_photo) і далі
   мають MAX_TRANSIENT_RETRIES=1 (тобто 2 спроби) при 30с — там короткий
   retry виправдовує себе більше, ніж для важкої генерації.

Публічні сигнатури (generate_text/generate_json/chat/chat_with_tools/
extract_receipt/transcribe_voice/analyze_product_photo) НЕ змінені.
"""

import asyncio
import json
import logging
import re
import time
import base64

import openai
from openai import AsyncOpenAI

from config.settings import (
    AI_API_KEY, AI_BASE_URL, AI_MODEL, AI_FALLBACK_MODELS,
    WHISPER_API_KEY, WHISPER_BASE_URL, WHISPER_MODEL,
)

logger = logging.getLogger("tasks_bot")

# Таймаут для ЗВИЧАЙНИХ (легких) запитів: chat, розбір чека, аналіз фото товару.
AI_REQUEST_TIMEOUT_SECONDS = 30

# ЗМІНЕНО (нове): окремий, довший таймаут ЛИШЕ для label="generate" —
# генерація повного сайту (HTML+CSS+JS) одним запитом об'єктивно триваліша,
# ніж звичайні текстові/JSON відповіді, і 30с їй систематично не вистачало
# (див. логи: "AI timeout ... label=generate" повторювався щоразу рівно
# на позначці ~28-30с).
AI_GENERATE_TIMEOUT_SECONDS = 60

# ЗМІНЕНО: 40 → 90. Має залишатись БІЛЬШИМ за AI_GENERATE_TIMEOUT_SECONDS
# з запасом — інакше HTTP-клієнт сам обірве з'єднання раніше, ніж наш
# власний asyncio.wait_for(60с) встигне спрацювати.
AI_CLIENT_TIMEOUT_SECONDS = 90

# Скільки РАЗІВ ПОВТОРЮВАТИ ОДНУ Й ТУ Ж модель при ТИМЧАСОВІЙ помилці
# (таймаут / порожня відповідь) — стосується лише ЛЕГКИХ лейблів.
# Для label="generate" ретрай навмисно вимкнено (див. _effective_retry_budget).
MAX_TRANSIENT_RETRIES = 1

# Скільки секунд не звертатись до моделі, яка щойно впала з 429 (вичерпаний
# денний ліміт), якщо провайдер не підказав точний час скидання ліміту.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 600

client: AsyncOpenAI | None = (
    AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if AI_API_KEY else None
)
whisper_client: AsyncOpenAI | None = (
    AsyncOpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_BASE_URL, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if WHISPER_API_KEY else None
)

if not client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")
if not whisper_client:
    logger.warning("WHISPER_API_KEY не задано — розпізнавання голосових повідомлень вимкнено")

_model_verified = False

_SAFETY_LINE_RE = re.compile(
    r"^\s*(user|response|input|output|prompt)\s*safety\s*:\s*\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)

MAX_IMAGES_PER_REQUEST = 10

# ---------------------------------------------------------------
# Стан по моделях (in-memory, на весь час життя процесу):
# - _unavailable_until: які моделі зараз "на паузі" після 429 і до якого моменту;
# - _no_json_support: які моделі, як з'ясувалось емпірично, не приймають
#   response_format=json_object — щоб не тестувати це щоразу заново.
# ---------------------------------------------------------------
_unavailable_until: dict[str, float] = {}
_no_json_support: set[str] = set()


def _model_list() -> list[str]:
    """AI_MODEL завжди першим, далі — унікальні fallback-моделі з .env
    (AI_FALLBACK_MODELS), у порядку зазначення. Немає жорсткої прив'язки
    коду до конкретної назви моделі — все конфігурується через .env.

    ⚠️ ВАЖЛИВО: наразі за замовчуванням AI_FALLBACK_MODELS порожній
    (у логах видно "моделі=['openrouter/free']" — тобто лише ОДНА
    модель). Це означає, що коли openrouter/free перевантажена, боту
    просто немає на що переключитись. Якщо додати сюди хоча б одну
    запасну модель через .env (AI_FALLBACK_MODELS), код автоматично
    почне її використовувати при відмові першої — жодних змін коду для
    цього більше не потрібно, лише конфігурація.

    ⚠️ ПРО ТАЙМ-АУТИ: кожна додаткова fallback-модель у цьому списку
    пропорційно збільшує найгірший сценарій часу відповіді (кожна
    модель — це ще один цикл спроб). Якщо задаси AI_FALLBACK_MODELS з
    кількома моделями, для label="generate" це буде +60с (json_mode) на
    кожну додаткову модель — можливо, доведеться підняти зовнішній
    тайм-аут очікування відповіді там, де викликається generate_json()
    для сайтів (наразі в handlers/website_builder.py немає окремого
    AI_PLAN_TIMEOUT_SECONDS-подібного обмеження, але якщо воно з'явиться —
    врахуй це)."""
    models = [AI_MODEL] if AI_MODEL else []
    for m in AI_FALLBACK_MODELS:
        if m and m not in models:
            models.append(m)
    return models


def _mark_unavailable(model: str, seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS) -> None:
    _unavailable_until[model] = time.monotonic() + max(seconds, 30.0)


def _is_unavailable(model: str) -> bool:
    until = _unavailable_until.get(model)
    return bool(until and until > time.monotonic())


def _cooldown_from_error(exc: Exception) -> float | None:
    """OpenRouter часто повертає X-RateLimit-Reset (epoch мс) у заголовках
    помилки — якщо він є, чекаємо саме до цього моменту, а не навмання."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response else None
    if not headers:
        return None
    raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if not raw:
        return None
    try:
        reset_epoch_seconds = float(raw) / 1000.0
        return max(reset_epoch_seconds - time.time(), 30.0)
    except (TypeError, ValueError):
        return None


def _strip_safety_noise(text: str) -> str:
    if not text:
        return text
    cleaned = _SAFETY_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _timeout_for_label(label: str) -> float:
    """НОВЕ: важкі задачі (генерація сайту) отримують суттєво більше часу
    на одну спробу, ніж легкі текстові/JSON запити."""
    return AI_GENERATE_TIMEOUT_SECONDS if label == "generate" else AI_REQUEST_TIMEOUT_SECONDS


def _effective_retry_budget(label: str, allow_retry: bool) -> int:
    """НОВЕ: скільки РАЗІВ ПОВТОРИТИ одну й ту ж модель. Для важкого
    label="generate" ретрай навмисно вимкнено (0) — краще одна спроба з
    великим вікном часу (AI_GENERATE_TIMEOUT_SECONDS), ніж дві короткі,
    жодна з яких не встигає. Для решти лейблів — як і раніше."""
    if label == "generate":
        return 0
    return MAX_TRANSIENT_RETRIES if allow_retry else 0


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
        models = await asyncio.wait_for(client.models.list(), timeout=AI_REQUEST_TIMEOUT_SECONDS)
        slugs = {m.id for m in models.data}
        configured = _model_list()
        missing = [m for m in configured if m not in slugs]
        if missing:
            logger.warning("Ці AI-моделі з конфігурації не знайдено у провайдера: %s", missing)
        else:
            logger.info("Усі налаштовані AI-моделі підтверджено провайдером: %s", configured)
        _model_verified = True
        return not missing
    except Exception:
        logger.exception("Не вдалося перевірити список моделей AI-провайдера")
        return False


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```")
    return raw.strip()


def _extract_json_object(raw: str) -> str | None:
    """Шукає перший ЗБАЛАНСОВАНИЙ {...} блок у тексті — навіть якщо модель
    додала пояснювальний текст до/після JSON (частий випадок у безкоштовних
    моделей)."""
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


def _try_parse_json_dict(raw: str | None) -> dict | None:
    """Єдина точка парсингу JSON-словника з сирої відповіді моделі.
    Повертає None замість того, щоб кидати виняток, якщо парсинг не вдався —
    виклик generate_json вирішує, чи варто повторити запит інакше."""
    if not raw or not raw.strip():
        return None
    candidate = _extract_json_object(raw) or _strip_json_fence(raw)
    if not candidate or not candidate.strip():
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_content(prompt: str, images: list[str] | None):
    if not images:
        return prompt
    content = [{"type": "text", "text": prompt}]
    for url in images[:MAX_IMAGES_PER_REQUEST]:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


async def _call_once(model: str, messages: list[dict], temperature: float, json_mode: bool, timeout: float):
    kwargs = {"temperature": temperature}
    if json_mode and model not in _no_json_support:
        kwargs["response_format"] = {"type": "json_object"}
    return await asyncio.wait_for(
        client.chat.completions.create(model=model, messages=messages, **kwargs),
        timeout=timeout,
    )


async def _chat_completion(
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    label: str = "request",
    allow_retry: bool = True,
) -> str | None:
    """
    ЗМІНЕНО: таймаут і бюджет ретраїв на одну модель тепер залежать від
    label (див. _timeout_for_label / _effective_retry_budget) — важка
    генерація сайту (label="generate") отримує довше вікно на спробу і
    без внутрішнього повтору, легкі запити — як і раніше, коротке вікно
    з одним повтором.
    """
    if not client:
        return None

    models = _model_list()
    if not models:
        logger.error("Немає жодної налаштованої AI-моделі (AI_MODEL порожній) — label=%s", label)
        return None

    timeout = _timeout_for_label(label)
    max_retries = _effective_retry_budget(label, allow_retry)

    for model in models:
        if _is_unavailable(model):
            logger.info("Модель %s на паузі після 429, пропускаю (label=%s)", model, label)
            continue

        for attempt in range(max_retries + 1):
            try:
                resp = await _call_once(model, messages, temperature, json_mode, timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "AI timeout (model=%s, label=%s, timeout=%.0fс, спроба=%s/%s)",
                    model, label, timeout, attempt + 1, max_retries + 1,
                )
                if attempt < max_retries:
                    continue
                break  # переходимо до наступної моделі
            except openai.RateLimitError as e:
                cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                _mark_unavailable(model, cooldown)
                logger.warning(
                    "AI 429 (rate limit) на моделі %s, пауза на %.0fс, пробую наступну модель (label=%s)",
                    model, cooldown, label,
                )
                break  # НЕ повторюємо ту саму модель
            except openai.BadRequestError:
                # Найчастіша причина — модель не приймає response_format=json_object.
                if json_mode and model not in _no_json_support:
                    _no_json_support.add(model)
                    logger.info(
                        "Модель %s не підтримує response_format=json_object, повторюю без нього (label=%s)",
                        model, label,
                    )
                    continue  # той самий attempt-бюджет, той самий запит, але тепер без json
                logger.exception("AI BadRequestError (model=%s, label=%s)", model, label)
                break
            except Exception:
                logger.exception("AI request failed (model=%s, label=%s)", model, label)
                break

            choice = resp.choices[0]
            raw = (choice.message.content or "").strip()
            if raw:
                logger.info("AI успішно відповів (model=%s, label=%s)", model, label)
                return _strip_safety_noise(raw)

            finish_reason = getattr(choice, "finish_reason", None)
            logger.warning(
                "AI повернув ПОРОЖНІЙ content (model=%s, label=%s, finish_reason=%s, спроба=%s/%s)",
                model, label, finish_reason, attempt + 1, max_retries + 1,
            )
            if attempt < max_retries:
                continue
            break  # наступна модель

    logger.error("Усі AI-моделі недоступні для запиту (label=%s, моделі=%s)", label, models)
    return None


async def _complete(
    prompt: str,
    temperature: float,
    json_mode: bool,
    images: list[str] | None = None,
    allow_retry: bool = True,
) -> str | None:
    content = _build_content(prompt, images)
    messages = [{"role": "user", "content": content}]
    return await _chat_completion(messages, temperature, json_mode, label="generate", allow_retry=allow_retry)


async def generate_text(prompt: str, temperature: float = 0.6) -> str | None:
    return await _complete(prompt, temperature, json_mode=False)


async def generate_json(prompt: str, temperature: float = 0.7, images: list[str] | None = None) -> dict | None:
    """Основний шлях: запит у json_mode. Якщо результат не парситься як
    JSON-об'єкт, робимо ОДИН додатковий запит у звичайному текстовому
    режимі. Обидва проходи для важкої генерації (label="generate")
    використовують довший AI_GENERATE_TIMEOUT_SECONDS і без внутрішнього
    повтору (див. _effective_retry_budget) — найгірший сценарій тепер
    ~2×AI_GENERATE_TIMEOUT_SECONDS на одну модель, а не 3-4× як було."""
    raw = await _complete(prompt, temperature, json_mode=True, images=images)
    data = _try_parse_json_dict(raw)
    if data is not None:
        return data

    if raw is not None:
        logger.warning("AI повернув JSON, який не вдалось розпарсити (json_mode), пробую текстовий режим. raw[:300]=%r", raw[:300])
    else:
        logger.warning("AI не повернув відповіді в json_mode, пробую текстовий режим.")

    raw_fallback = await _complete(prompt, temperature, json_mode=False, images=images, allow_retry=False)
    data = _try_parse_json_dict(raw_fallback)
    if data is not None:
        logger.info("Текстовий fallback-запит дав валідний JSON.")
        return data

    if raw_fallback is not None:
        logger.error("AI повернув некоректний JSON навіть у текстовому режимі: %s", raw_fallback[:300])
    else:
        logger.error("AI не відповів навіть у текстовому fallback-режимі.")
    return None


async def chat(messages: list[dict], temperature: float = 0.7) -> str | None:
    return await _chat_completion(messages, temperature, json_mode=False, label="chat")


async def chat_with_tools(messages: list[dict], tools: list[dict], temperature: float = 0.7):
    if not client:
        return None

    models = _model_list()
    for model in models:
        if _is_unavailable(model):
            continue
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto", temperature=temperature,
                ),
                timeout=AI_REQUEST_TIMEOUT_SECONDS,
            )
            return resp.choices[0].message
        except asyncio.TimeoutError:
            logger.warning("AI chat_with_tools timeout (model=%s)", model)
            continue
        except openai.RateLimitError as e:
            cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
            _mark_unavailable(model, cooldown)
            logger.warning("AI chat_with_tools 429 (model=%s), пробую наступну модель", model)
            continue
        except Exception:
            logger.warning("Модель %s не прийняла tools, пробую без них", model, exc_info=True)
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(model=model, messages=messages, temperature=temperature),
                    timeout=AI_REQUEST_TIMEOUT_SECONDS,
                )
                return resp.choices[0].message
            except Exception:
                logger.exception("AI chat_with_tools request failed (model=%s)", model)
                continue

    logger.error("Усі AI-моделі недоступні для chat_with_tools")
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

    raw = await _chat_completion(messages, temperature=0.2, json_mode=True, label="extract_receipt")
    data = _try_parse_json_dict(raw)
    if data is not None:
        return data
    if raw is not None:
        logger.error("AI повернув некоректний JSON для чека: %s", raw[:300])
    return None


async def transcribe_voice(audio_bytes: bytes) -> str | None:
    if not whisper_client:
        return None
    try:
        resp = await asyncio.wait_for(
            whisper_client.audio.transcriptions.create(
                model=WHISPER_MODEL, file=("voice.ogg", audio_bytes), language="uk",
            ),
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        text = (resp.text or "").strip()
        return text or None
    except asyncio.TimeoutError:
        logger.warning("Voice transcription timed out after %ss", AI_REQUEST_TIMEOUT_SECONDS)
        return None
    except Exception:
        logger.exception("Voice transcription failed")
        return None


async def analyze_product_photo(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict | None:
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

    raw = await _chat_completion(messages, temperature=0.4, json_mode=True, label="analyze_product_photo")
    data = _try_parse_json_dict(raw)
    if data is not None:
        return data
    if raw is not None:
        logger.error("AI повернув некоректний JSON для товару: %s", raw[:300])
    return None