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

ЗМІНИ (попередній прохід):
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
   має вдвічі більше часу (60с замість 30с).
4. Легкі лейбли (chat/extract_receipt/analyze_product_photo) і далі
   мають MAX_TRANSIENT_RETRIES=1 (тобто 2 спроби) при 30с.

НОВЕ (цей прохід): резервний AI-провайдер (AI_API_KEY_BACKUP).
- Раніше весь модуль працював з ОДНИМ клієнтом (`client`) і одним списком
  моделей (AI_MODEL + AI_FALLBACK_MODELS). Тепер це узагальнено до списку
  "провайдерів" (_Provider): спершу основний (AI_API_KEY/AI_BASE_URL,
  з усіма його fallback-моделями), і, якщо він заданий, — резервний
  (AI_API_KEY_BACKUP/AI_BASE_URL_BACKUP/AI_MODEL_BACKUP).
- Порядок спроб: усі моделі основного провайдера підряд (як і раніше) →
  якщо ЖОДНА не відповіла (таймаут/429/помилка авторизації/порожня
  відповідь) → усі моделі резервного провайдера.
- Якщо AI_API_KEY_BACKUP не задано — резервного провайдера просто немає
  в списку, і поведінка ідентична попередній версії.
- "Пауза" моделі після 429 (_unavailable_until) і позначка "не підтримує
  json_mode" (_no_json_support) тепер прив'язані до пари
  (провайдер, модель), а не лише до назви моделі — це важливо, якщо
  основний і резервний провайдер використовують модель з однаковою
  назвою (наприклад, той самий Gemini-фліт, але інший акаунт/ключ):
  пауза одного провайдера не повинна впливати на інший.
- Явно ловиться openai.AuthenticationError: якщо ключ провайдера
  невалідний/протермінований, весь цей провайдер одразу позначається
  недоступним (а не перебирається модель за моделлю з тим самим 401),
  і бот одразу переходить до наступного провайдера.
- Публічні сигнатури (generate_text/generate_json/chat/chat_with_tools/
  extract_receipt/transcribe_voice/analyze_product_photo/is_available)
  НЕ змінені — увесь fallback на резервний ключ прихований усередині
  модуля.
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
    AI_API_KEY_BACKUP, AI_BASE_URL_BACKUP, AI_MODEL_BACKUP,
    WHISPER_API_KEY, WHISPER_BASE_URL, WHISPER_MODEL,
)

logger = logging.getLogger("tasks_bot")

# Таймаут для ЗВИЧАЙНИХ (легких) запитів: chat, розбір чека, аналіз фото товару.
AI_REQUEST_TIMEOUT_SECONDS = 30

# Окремий, довший таймаут ЛИШЕ для label="generate" — генерація повного
# сайту (HTML+CSS+JS) одним запитом об'єктивно триваліша, ніж звичайні
# текстові/JSON відповіді, і 30с їй систематично не вистачало.
AI_GENERATE_TIMEOUT_SECONDS = 60

# Має залишатись БІЛЬШИМ за AI_GENERATE_TIMEOUT_SECONDS з запасом —
# інакше HTTP-клієнт сам обірве з'єднання раніше, ніж наш власний
# asyncio.wait_for(60с) встигне спрацювати.
AI_CLIENT_TIMEOUT_SECONDS = 90

# Скільки РАЗІВ ПОВТОРЮВАТИ ОДНУ Й ТУ Ж модель при ТИМЧАСОВІЙ помилці
# (таймаут / порожня відповідь) — стосується лише ЛЕГКИХ лейблів.
# Для label="generate" ретрай навмисно вимкнено (див. _effective_retry_budget).
MAX_TRANSIENT_RETRIES = 1

# Скільки секунд не звертатись до моделі, яка щойно впала з 429 (вичерпаний
# денний ліміт), якщо провайдер не підказав точний час скидання ліміту.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 600

# Скільки секунд не звертатись до ВСЬОГО провайдера після помилки
# авторизації (невірний/протермінований ключ) — довше, ніж звичайний
# rate-limit cooldown, бо сама причина інша й навряд чи "розсмокчеться"
# за 10 хвилин.
AUTH_ERROR_COOLDOWN_SECONDS = 1800


def _dedup(models: list[str | None]) -> list[str]:
    result: list[str] = []
    for m in models:
        if m and m not in result:
            result.append(m)
    return result


class _Provider:
    """Один AI-провайдер: свій ключ, свій base_url, свій список моделей
    (перша — основна, решта — fallback у межах ЦЬОГО провайдера)."""

    __slots__ = ("name", "base_url", "models", "client")

    def __init__(self, name: str, api_key: str, base_url: str, models: list[str]):
        self.name = name
        self.base_url = base_url
        self.models = models
        self.client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
            if api_key and models else None
        )


_primary_provider = _Provider(
    "primary", AI_API_KEY, AI_BASE_URL, _dedup([AI_MODEL, *AI_FALLBACK_MODELS])
)
_backup_provider = _Provider(
    "backup", AI_API_KEY_BACKUP, AI_BASE_URL_BACKUP, _dedup([AI_MODEL_BACKUP])
)

# Порядок важливий: спершу основний провайдер (з усіма його fallback-
# моделями), і лише якщо він ЦІЛКОМ не відповів — резервний.
_providers: list[_Provider] = [p for p in (_primary_provider, _backup_provider) if p.client]

# Збережено для зворотної сумісності з рештою коду (is_available(),
# верифікація моделей тощо) — це клієнт ОСНОВНОГО провайдера.
client: AsyncOpenAI | None = _primary_provider.client
whisper_client: AsyncOpenAI | None = (
    AsyncOpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_BASE_URL, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if WHISPER_API_KEY else None
)

if not _primary_provider.client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")
if _backup_provider.client:
    logger.info(
        "Резервний AI-провайдер підключено (backup моделі=%s, base_url=%s) — "
        "буде використано, якщо основний провайдер повністю недоступний",
        _backup_provider.models, _backup_provider.base_url,
    )
if not whisper_client:
    logger.warning("WHISPER_API_KEY не задано — розпізнавання голосових повідомлень вимкнено")

_model_verified = False

_SAFETY_LINE_RE = re.compile(
    r"^\s*(user|response|input|output|prompt)\s*safety\s*:\s*\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)

MAX_IMAGES_PER_REQUEST = 10

# ---------------------------------------------------------------
# Стан по (провайдер, модель) — in-memory, на весь час життя процесу:
# - _unavailable_until: які пари зараз "на паузі" (після 429 або 401) і
#   до якого моменту;
# - _no_json_support: які пари, як з'ясувалось емпірично, не приймають
#   response_format=json_object — щоб не тестувати це щоразу заново.
# Ключ — tuple (provider_name, model), щоб пауза одного провайдера не
# впливала на модель з такою самою назвою в іншого провайдера.
# ---------------------------------------------------------------
_unavailable_until: dict[tuple[str, str], float] = {}
_no_json_support: set[tuple[str, str]] = set()


def _mark_unavailable(key: tuple[str, str], seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS) -> None:
    _unavailable_until[key] = time.monotonic() + max(seconds, 30.0)


def _is_unavailable(key: tuple[str, str]) -> bool:
    until = _unavailable_until.get(key)
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
    """Важкі задачі (генерація сайту) отримують суттєво більше часу на
    одну спробу, ніж легкі текстові/JSON запити."""
    return AI_GENERATE_TIMEOUT_SECONDS if label == "generate" else AI_REQUEST_TIMEOUT_SECONDS


def _effective_retry_budget(label: str, allow_retry: bool) -> int:
    """Скільки РАЗІВ ПОВТОРИТИ одну й ту ж модель. Для важкого
    label="generate" ретрай навмисно вимкнено (0) — краще одна спроба з
    великим вікном часу (AI_GENERATE_TIMEOUT_SECONDS), ніж дві короткі,
    жодна з яких не встигає. Для решти лейблів — як і раніше."""
    if label == "generate":
        return 0
    return MAX_TRANSIENT_RETRIES if allow_retry else 0


def is_available() -> bool:
    return bool(_providers)


def voice_available() -> bool:
    return whisper_client is not None


async def verify_model() -> bool:
    """Перевіряє список моделей у КОЖНОГО налаштованого провайдера
    (основного і, якщо є, резервного)."""
    global _model_verified
    if not _providers:
        return False
    if _model_verified:
        return True

    all_ok = True
    for provider in _providers:
        try:
            models = await asyncio.wait_for(provider.client.models.list(), timeout=AI_REQUEST_TIMEOUT_SECONDS)
            slugs = {m.id for m in models.data}
            missing = [m for m in provider.models if m not in slugs]
            if missing:
                logger.warning(
                    "Провайдер %s: ці AI-моделі з конфігурації не знайдено у провайдера: %s",
                    provider.name, missing,
                )
                all_ok = False
            else:
                logger.info(
                    "Провайдер %s: усі налаштовані AI-моделі підтверджено провайдером: %s",
                    provider.name, provider.models,
                )
        except Exception:
            logger.exception("Не вдалося перевірити список моделей провайдера %s", provider.name)
            all_ok = False

    _model_verified = True
    return all_ok


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


async def _call_once(
    provider_client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    timeout: float,
    key: tuple[str, str],
):
    kwargs = {"temperature": temperature}
    if json_mode and key not in _no_json_support:
        kwargs["response_format"] = {"type": "json_object"}
    return await asyncio.wait_for(
        provider_client.chat.completions.create(model=model, messages=messages, **kwargs),
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
    Перебирає провайдерів по черзі (основний, потім резервний, якщо
    заданий), а всередині кожного — його моделі по черзі. Таймаут і
    бюджет ретраїв на одну модель залежать від label (див.
    _timeout_for_label / _effective_retry_budget).
    """
    if not _providers:
        return None

    timeout = _timeout_for_label(label)
    max_retries = _effective_retry_budget(label, allow_retry)

    for provider in _providers:
        for model in provider.models:
            key = (provider.name, model)
            if _is_unavailable(key):
                logger.info(
                    "%s/%s на паузі, пропускаю (label=%s)", provider.name, model, label,
                )
                continue

            for attempt in range(max_retries + 1):
                try:
                    resp = await _call_once(provider.client, model, messages, temperature, json_mode, timeout, key)
                except asyncio.TimeoutError:
                    logger.warning(
                        "AI timeout (provider=%s, model=%s, label=%s, timeout=%.0fс, спроба=%s/%s)",
                        provider.name, model, label, timeout, attempt + 1, max_retries + 1,
                    )
                    if attempt < max_retries:
                        continue
                    break  # переходимо до наступної моделі/провайдера
                except openai.AuthenticationError:
                    logger.error(
                        "AI провайдер %s відповів помилкою авторизації (невірний/протермінований "
                        "ключ) — пропускаю весь провайдер на %.0fс (label=%s)",
                        provider.name, AUTH_ERROR_COOLDOWN_SECONDS, label,
                    )
                    for m in provider.models:
                        _mark_unavailable((provider.name, m), AUTH_ERROR_COOLDOWN_SECONDS)
                    break
                except openai.RateLimitError as e:
                    cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                    _mark_unavailable(key, cooldown)
                    logger.warning(
                        "AI 429 (rate limit) на %s/%s, пауза на %.0fс, пробую наступну модель/провайдера (label=%s)",
                        provider.name, model, cooldown, label,
                    )
                    break  # НЕ повторюємо ту саму модель
                except openai.BadRequestError:
                    # Найчастіша причина — модель не приймає response_format=json_object.
                    if json_mode and key not in _no_json_support:
                        _no_json_support.add(key)
                        logger.info(
                            "%s/%s не підтримує response_format=json_object, повторюю без нього (label=%s)",
                            provider.name, model, label,
                        )
                        continue  # той самий attempt-бюджет, той самий запит, але тепер без json
                    logger.exception("AI BadRequestError (provider=%s, model=%s, label=%s)", provider.name, model, label)
                    break
                except Exception:
                    logger.exception("AI request failed (provider=%s, model=%s, label=%s)", provider.name, model, label)
                    break

                choice = resp.choices[0]
                raw = (choice.message.content or "").strip()
                if raw:
                    logger.info("AI успішно відповів (provider=%s, model=%s, label=%s)", provider.name, model, label)
                    return _strip_safety_noise(raw)

                finish_reason = getattr(choice, "finish_reason", None)
                logger.warning(
                    "AI повернув ПОРОЖНІЙ content (provider=%s, model=%s, label=%s, finish_reason=%s, спроба=%s/%s)",
                    provider.name, model, label, finish_reason, attempt + 1, max_retries + 1,
                )
                if attempt < max_retries:
                    continue
                break  # наступна модель/провайдер

    provider_names = [p.name for p in _providers]
    logger.error(
        "Усі AI-провайдери недоступні для запиту (label=%s, провайдери=%s)", label, provider_names,
    )
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
    повтору (див. _effective_retry_budget)."""
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
    if not _providers:
        return None

    for provider in _providers:
        for model in provider.models:
            key = (provider.name, model)
            if _is_unavailable(key):
                continue
            try:
                resp = await asyncio.wait_for(
                    provider.client.chat.completions.create(
                        model=model, messages=messages, tools=tools, tool_choice="auto", temperature=temperature,
                    ),
                    timeout=AI_REQUEST_TIMEOUT_SECONDS,
                )
                return resp.choices[0].message
            except asyncio.TimeoutError:
                logger.warning("AI chat_with_tools timeout (provider=%s, model=%s)", provider.name, model)
                continue
            except openai.AuthenticationError:
                logger.error(
                    "AI chat_with_tools: провайдер %s відповів помилкою авторизації, пропускаю провайдер",
                    provider.name,
                )
                for m in provider.models:
                    _mark_unavailable((provider.name, m), AUTH_ERROR_COOLDOWN_SECONDS)
                break
            except openai.RateLimitError as e:
                cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                _mark_unavailable(key, cooldown)
                logger.warning("AI chat_with_tools 429 (provider=%s, model=%s), пробую наступну модель", provider.name, model)
                continue
            except Exception:
                logger.warning("Провайдер %s / модель %s не прийняла tools, пробую без них", provider.name, model, exc_info=True)
                try:
                    resp = await asyncio.wait_for(
                        provider.client.chat.completions.create(model=model, messages=messages, temperature=temperature),
                        timeout=AI_REQUEST_TIMEOUT_SECONDS,
                    )
                    return resp.choices[0].message
                except Exception:
                    logger.exception("AI chat_with_tools request failed (provider=%s, model=%s)", provider.name, model)
                    continue

    logger.error("Усі AI-провайдери недоступні для chat_with_tools")
    return None


async def extract_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict | None:
    if not _providers:
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
    if not _providers:
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