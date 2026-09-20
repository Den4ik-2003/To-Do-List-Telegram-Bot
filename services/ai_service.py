"""
services/ai_service.py

ЩО ЗМІНЕНО В ЦЬОМУ ПРОХОДІ (безкоштовні моделі OpenRouter "вмирають" постійно):

1. Мертва модель більше не вбиває запит. 404 ("model unavailable for free",
   "No endpoints found") → модель одразу йде на паузу на 6 годин, без
   traceback і без повторних спроб, і бот переходить до наступної.
   Так само коротко обробляються 402/403 (немає кредитів/прав) та 5xx.
2. Авто-добір живих безкоштовних моделей. Для провайдерів на openrouter.ai
   раз на AI_FREE_MODELS_REFRESH_SECONDS завантажується публічний каталог
   /models, з нього беруться моделі з нульовою ціною, і вони ранжуються
   (розмір, контекст, підтримка JSON-режиму). Моделі з AI_MODEL /
   AI_FALLBACK_MODELS пробуються ПЕРШИМИ, далі — авто-список, в кінці —
   роутер openrouter/free. Тобто навіть зі старими слагами в env бот
   самолікується. Вимкнути: AI_AUTO_FREE_MODELS=false.
3. Генерація сайту йде стрімінгом (label="generate"). Замість одного
   жорсткого таймауту на всю відповідь — таймаут "мовчання" (AI_STREAM_
   IDLE_TIMEOUT_SECONDS) + загальна стеля AI_GENERATE_TIMEOUT_SECONDS. Повільна,
   але жива модель встигає дописати довгий сайт, а зависла відсікається за 60с.
4. Обрізана відповідь (finish_reason=length) або зламаний JSON не
   вважається успіхом — бот пробує наступну модель, а не віддає "битий" сайт.
   Для генерації виставляється max_tokens (з каталогу моделі), парсинг JSON
   толерантний до переносів рядків усередині рядків (strict=False), а
   <think>…</think> зі "міркуючих" моделей вирізається.
5. Ліміт моделей на запит: AI_MAX_MODELS_PER_REQUEST рахує лише ПОВІЛЬНІ
   відмови (таймаут/порожньо/обрізано/зламаний JSON), миттєві (404/429/401)
   не рахуються. Для запитів із фото обираються моделі з підтримкою зображень.
6. Дубльований провайдер (той самий ключ+URL) не викликається двічі за запит.

Публічні сигнатури (generate_text / generate_json / chat / chat_with_tools /
extract_receipt / transcribe_voice / analyze_product_photo / is_available /
voice_available / verify_model) НЕ змінені.
"""

import asyncio
import base64
import json
import logging
import re
import time

import aiohttp
import openai
from openai import AsyncOpenAI

from config.settings import (
    AI_API_KEY, AI_BASE_URL, AI_MODEL, AI_FALLBACK_MODELS,
    AI_API_KEY_BACKUP, AI_BASE_URL_BACKUP, AI_MODEL_BACKUP,
    WHISPER_API_KEY, WHISPER_BASE_URL, WHISPER_MODEL,
    AI_REQUEST_TIMEOUT_SECONDS, AI_GENERATE_TIMEOUT_SECONDS, AI_STREAM_IDLE_TIMEOUT_SECONDS,
    AI_GENERATE_MAX_TOKENS, AI_MAX_MODELS_PER_REQUEST,
    AI_AUTO_FREE_MODELS, AI_FREE_MODELS_REFRESH_SECONDS, AI_FREE_MODELS_MAX,
    AI_FREE_MODELS_MIN_CONTEXT,
)

logger = logging.getLogger("tasks_bot")

# HTTP-клієнт має жити довше за найдовшу нашу власну спробу.
AI_CLIENT_TIMEOUT_SECONDS = AI_GENERATE_TIMEOUT_SECONDS + 30

# Повторів ТІЄЇ Ж моделі при тимчасовій помилці (лише легкі лейбли).
MAX_TRANSIENT_RETRIES = 1

DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 600
AUTH_ERROR_COOLDOWN_SECONDS = 1800
MODEL_GONE_COOLDOWN_SECONDS = 6 * 3600     # 404: модель зникла / більше не безкоштовна
ACCESS_ERROR_COOLDOWN_SECONDS = 1800       # 402/403: немає кредитів або прав
SERVER_ERROR_COOLDOWN_SECONDS = 90         # 5xx / помилка в тілі відповіді

FREE_MODELS_RETRY_SECONDS = 120            # не долбити каталог, якщо він недоступний
MAX_IMAGES_PER_REQUEST = 10

# Результати спроби однієї моделі.
_OK = "ok"
_INSTANT = "instant"            # миттєва відмова (404/429/…): не рахується в ліміт моделей
_SLOW = "slow"                  # повільна відмова (таймаут/порожньо/обрізано/…)
_PROVIDER_DOWN = "provider_down"


def _dedup(models: list[str | None]) -> list[str]:
    result: list[str] = []
    for m in models:
        if m and m not in result:
            result.append(m)
    return result


class _Provider:
    """Один AI-провайдер: ключ, base_url, список моделей із конфігурації
    (перша — основна). Для OpenRouter додатково працює авто-добір
    безкоштовних моделей (auto_free)."""

    __slots__ = ("name", "base_url", "api_key", "models", "client", "auto_free")

    def __init__(self, name: str, api_key: str, base_url: str, models: list[str]):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.models = models
        self.auto_free = bool(AI_AUTO_FREE_MODELS and "openrouter.ai" in (base_url or "").lower())
        self.client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
            if api_key and (models or self.auto_free) else None
        )

    @property
    def identity(self) -> tuple[str, str]:
        return ((self.base_url or "").rstrip("/"), self.api_key)


_primary_provider = _Provider(
    "primary", AI_API_KEY, AI_BASE_URL, _dedup([AI_MODEL, *AI_FALLBACK_MODELS])
)
_backup_provider = _Provider(
    "backup", AI_API_KEY_BACKUP, AI_BASE_URL_BACKUP, _dedup([AI_MODEL_BACKUP])
)

# Спершу основний провайдер, і лише якщо він ЦІЛКОМ не відповів — резервний.
_providers: list[_Provider] = [p for p in (_primary_provider, _backup_provider) if p.client]

# Резервний провайдер з ТИМ САМИМ ключем і URL — це той самий акаунт: не
# дублюємо запити, а просто додаємо його модель у список основного.
if _backup_provider.client and _primary_provider.client and _backup_provider.identity == _primary_provider.identity:
    _primary_provider.models = _dedup([*_primary_provider.models, *_backup_provider.models])
    _providers = [_primary_provider]
    _backup_provider.client = None

# Для зворотної сумісності з рештою коду.
client: AsyncOpenAI | None = _primary_provider.client
whisper_client: AsyncOpenAI | None = (
    AsyncOpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_BASE_URL, timeout=AI_CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if WHISPER_API_KEY else None
)

if not _primary_provider.client:
    logger.warning("AI_API_KEY не задано — AI-функції вимкнено, решта бота працює як завжди")
if _backup_provider.client:
    logger.info(
        "Резервний AI-провайдер підключено (backup моделі=%s, base_url=%s)",
        _backup_provider.models, _backup_provider.base_url,
    )
if not whisper_client:
    logger.warning("WHISPER_API_KEY не задано — розпізнавання голосових повідомлень вимкнено")

_model_verified = False

_SAFETY_LINE_RE = re.compile(
    r"^\s*(user|response|input|output|prompt)\s*safety\s*:\s*\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)

# ---------------------------------------------------------------
# Стан по (провайдер, модель) — in-memory
# ---------------------------------------------------------------
_unavailable_until: dict[tuple[str, str], float] = {}
_no_json_support: set[tuple[str, str]] = set()


def _mark_unavailable(key: tuple[str, str], seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS) -> None:
    _unavailable_until[key] = time.monotonic() + max(seconds, 30.0)


def _is_unavailable(key: tuple[str, str]) -> bool:
    until = _unavailable_until.get(key)
    return bool(until and until > time.monotonic())


def _cooldown_from_error(exc: Exception) -> float | None:
    """OpenRouter часто повертає X-RateLimit-Reset (epoch мс) — чекаємо до нього."""
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


def _short_error(exc: Exception) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    return str(msg).replace("\n", " ")[:220]


# ---------------------------------------------------------------
# Авто-добір безкоштовних моделей OpenRouter
# ---------------------------------------------------------------
_free_ranked: list[str] = []
_model_meta: dict[str, dict] = {}
_free_models_fetched_at = 0.0
_free_models_last_attempt = 0.0
_free_models_lock = asyncio.Lock()

_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")
_SKIP_ID_PARTS = ("embed", "guard", "moderation")


def _is_zero(value) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _guess_size_b(*texts: str) -> float | None:
    """Розмір моделі в мільярдах параметрів із назви ('gpt-oss-120b' → 120)."""
    for text in texts:
        sizes = [float(x) for x in _SIZE_RE.findall((text or "").lower())]
        if sizes:
            return max(sizes)
    return None


def _score_free_model(size_b: float | None, ctx: int, params: set[str]) -> float:
    size = size_b if size_b else 30.0
    score = min(size, 250.0) / 10.0
    score += min(ctx / 32768.0, 8.0)
    if "response_format" in params or "structured_outputs" in params:
        score += 10.0
    if "reasoning" in params or "include_reasoning" in params:
        score -= 3.0   # "міркуючі" моделі довше відповідають — для генерації гірше
    return score


def _parse_free_models(payload: dict) -> tuple[list[str], dict[str, dict]]:
    scored: list[tuple[float, str]] = []
    meta: dict[str, dict] = {}
    for m in (payload.get("data") or []):
        try:
            mid = m.get("id")
            if not mid or mid.startswith("openrouter/"):
                continue
            if any(part in mid.lower() for part in _SKIP_ID_PARTS):
                continue
            pricing = m.get("pricing") or {}
            if not (_is_zero(pricing.get("prompt")) and _is_zero(pricing.get("completion"))):
                continue
            arch = m.get("architecture") or {}
            out_mods = arch.get("output_modalities") or ["text"]
            if "text" not in out_mods:
                continue
            in_mods = arch.get("input_modalities") or ["text"]
            ctx = int(m.get("context_length") or 0)
            if ctx and ctx < AI_FREE_MODELS_MIN_CONTEXT:
                continue
            raw_params = m.get("supported_parameters")
            params = set(raw_params or [])
            top = m.get("top_provider") or {}
            max_comp = top.get("max_completion_tokens")
            size_b = _guess_size_b(mid, m.get("name", ""))

            meta[mid] = {
                "ctx": ctx,
                "max_completion": int(max_comp) if max_comp else None,
                "vision": "image" in in_mods,
                "json": None if raw_params is None else ("response_format" in params or "structured_outputs" in params),
                "tools": None if raw_params is None else ("tools" in params),
            }
            scored.append((_score_free_model(size_b, ctx, params), mid))
        except Exception:
            continue

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [mid for _, mid in scored], meta


async def _fetch_models_payload(base_url: str) -> dict | None:
    url = base_url.rstrip("/") + "/models"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Accept": "application/json"}) as resp:
                if resp.status != 200:
                    logger.warning("Каталог моделей %s повернув HTTP %s", url, resp.status)
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("Не вдалося завантажити каталог моделей %s", url, exc_info=True)
        return None


def _openrouter_base_url() -> str | None:
    for p in _providers:
        if p.auto_free:
            return p.base_url
    return None


async def _maybe_refresh_free_models(force: bool = False) -> None:
    global _free_ranked, _model_meta, _free_models_fetched_at, _free_models_last_attempt
    base = _openrouter_base_url()
    if not base:
        return

    def _fresh() -> bool:
        return bool(_free_ranked) and (time.monotonic() - _free_models_fetched_at) < AI_FREE_MODELS_REFRESH_SECONDS

    if not force:
        if _fresh():
            return
        if (time.monotonic() - _free_models_last_attempt) < FREE_MODELS_RETRY_SECONDS:
            return

    async with _free_models_lock:
        if not force and _fresh():
            return
        _free_models_last_attempt = time.monotonic()
        payload = await _fetch_models_payload(base)
        if not payload:
            return
        ranked, meta = _parse_free_models(payload)
        if not ranked:
            logger.warning("У каталозі OpenRouter не знайдено безкоштовних моделей, що підходять")
            return
        _free_ranked, _model_meta, _free_models_fetched_at = ranked, meta, time.monotonic()
        logger.info(
            "Оновлено список безкоштовних моделей OpenRouter (%s шт.), топ-5: %s",
            len(ranked), ranked[:5],
        )


def _meta_ok(model: str, needs_vision: bool, needs_tools: bool) -> bool:
    meta = _model_meta.get(model)
    if not meta:
        return True   # невідома модель (напр. платна з env) — довіряємо конфігу
    if needs_vision and not meta.get("vision"):
        return False
    if needs_tools and meta.get("tools") is False:
        return False
    return True


def _json_allowed(key: tuple[str, str], model: str) -> bool:
    if key in _no_json_support:
        return False
    meta = _model_meta.get(model)
    if meta and meta.get("json") is False:
        return False
    return True


def _max_tokens_for(model: str, label: str) -> int | None:
    if label != "generate":
        return None
    meta = _model_meta.get(model)
    if not meta:
        return None
    limit = meta.get("max_completion")
    if limit:
        return min(AI_GENERATE_MAX_TOKENS, int(limit))
    ctx = meta.get("ctx")
    if ctx:
        return min(AI_GENERATE_MAX_TOKENS, max(int(ctx) // 4, 2048))
    return None


def _candidates(provider: _Provider, needs_vision: bool = False, needs_tools: bool = False) -> list[str]:
    """Порядок: моделі з конфігу → авто-список живих безкоштовних →
    роутер openrouter/free (лише для OpenRouter)."""
    names = list(provider.models)
    if provider.auto_free:
        auto = [
            m for m in _free_ranked
            if _meta_ok(m, needs_vision, needs_tools) and not _is_unavailable((provider.name, m))
        ][:AI_FREE_MODELS_MAX]
        names += auto
        names.append("openrouter/free")
    return [m for m in _dedup(names) if _meta_ok(m, needs_vision, needs_tools)]


# ---------------------------------------------------------------
# Очищення й парсинг відповіді
# ---------------------------------------------------------------

def _strip_safety_noise(text: str) -> str:
    if not text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _SAFETY_LINE_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```")
    return raw.strip()


def _extract_json_object(raw: str) -> str | None:
    """Перший ЗБАЛАНСОВАНИЙ {...} блок — навіть якщо модель додала текст до/після."""
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
    """Єдина точка парсингу JSON-словника з відповіді моделі. strict=False
    дозволяє переноси рядків усередині рядкових значень — слабкі моделі
    постійно так ламають JSON із HTML."""
    if not raw or not raw.strip():
        return None
    candidate = _extract_json_object(raw) or _strip_json_fence(raw)
    if not candidate or not candidate.strip():
        return None
    try:
        data = json.loads(candidate, strict=False)
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


def _timeout_for_label(label: str) -> float:
    return AI_GENERATE_TIMEOUT_SECONDS if label == "generate" else AI_REQUEST_TIMEOUT_SECONDS


def _effective_retry_budget(label: str, allow_retry: bool) -> int:
    """Для важкого label="generate" повтор тієї ж моделі вимкнено —
    краще перейти до наступної моделі."""
    if label == "generate":
        return 0
    return MAX_TRANSIENT_RETRIES if allow_retry else 0


def is_available() -> bool:
    return bool(_providers)


def voice_available() -> bool:
    return whisper_client is not None


async def verify_model() -> bool:
    """Оновлює список безкоштовних моделей і перевіряє моделі з конфігурації."""
    global _model_verified
    if not _providers:
        return False

    await _maybe_refresh_free_models(force=True)

    if _model_verified:
        return True

    all_ok = True
    for provider in _providers:
        if not provider.models:
            continue
        try:
            models = await asyncio.wait_for(provider.client.models.list(), timeout=AI_REQUEST_TIMEOUT_SECONDS)
            slugs: set[str] = set()
            for m in models.data:
                slugs.add(m.id)
                slugs.add(m.id.removeprefix("models/"))
            missing = [m for m in provider.models if m not in slugs]
            if missing:
                logger.warning(
                    "Провайдер %s: ці AI-моделі з конфігурації не знайдено (пропущу їх, "
                    "працюватиме авто-добір): %s", provider.name, missing,
                )
                all_ok = False
            else:
                logger.info("Провайдер %s: усі налаштовані AI-моделі підтверджено: %s", provider.name, provider.models)
        except Exception:
            logger.exception("Не вдалося перевірити список моделей провайдера %s", provider.name)
            all_ok = False

    _model_verified = True
    return all_ok


# ---------------------------------------------------------------
# Виклик однієї моделі
# ---------------------------------------------------------------

def _describe_error(obj) -> str | None:
    err = getattr(obj, "error", None)
    if not err:
        return None
    return str(err).replace("\n", " ")[:200]


async def _collect_stream(client: AsyncOpenAI, model: str, messages: list[dict], kwargs: dict, total_timeout: float):
    """Читає відповідь стрімом. Таймаут — на "мовчання" між шматками
    (і на очікування першого), плюс загальна стеля."""
    deadline = time.monotonic() + total_timeout
    idle = float(AI_STREAM_IDLE_TIMEOUT_SECONDS)
    stream = await asyncio.wait_for(
        client.chat.completions.create(model=model, messages=messages, stream=True, **kwargs),
        timeout=min(idle, total_timeout),
    )
    parts: list[str] = []
    finish: str | None = None
    error: str | None = None
    try:
        iterator = stream.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=min(idle, remaining))
            except StopAsyncIteration:
                break
            error = _describe_error(chunk)
            if error:
                finish = "error"
                break
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            ch = choices[0]
            delta = getattr(ch, "delta", None)
            piece = getattr(delta, "content", None) if delta is not None else None
            if piece:
                parts.append(piece)
            fr = getattr(ch, "finish_reason", None)
            if fr:
                finish = fr
    finally:
        try:
            await stream.close()
        except Exception:
            pass
    return "".join(parts), finish, error


async def _call_model(
    provider_client: AsyncOpenAI, model: str, messages: list[dict], temperature: float,
    use_json: bool, timeout: float, max_tokens: int | None, stream: bool,
) -> tuple[str, str | None, str | None]:
    """Повертає (текст, finish_reason, помилка_в_тілі)."""
    kwargs: dict = {"temperature": temperature}
    if use_json:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    if stream:
        return await _collect_stream(provider_client, model, messages, kwargs, timeout)

    resp = await asyncio.wait_for(
        provider_client.chat.completions.create(model=model, messages=messages, **kwargs),
        timeout=timeout,
    )
    choices = getattr(resp, "choices", None)
    if not choices:
        return "", None, _describe_error(resp) or "порожній choices"
    choice = choices[0]
    message = getattr(choice, "message", None)
    return (getattr(message, "content", None) or ""), getattr(choice, "finish_reason", None), None


async def _run_model(
    provider: _Provider, model: str, messages: list[dict], temperature: float,
    json_mode: bool, expect_json: bool, label: str, allow_retry: bool,
) -> tuple[str | None, str]:
    key = (provider.name, model)
    timeout = _timeout_for_label(label)
    transient_left = _effective_retry_budget(label, allow_retry)
    use_json = json_mode and _json_allowed(key, model)
    max_tokens = _max_tokens_for(model, label)
    stream = label == "generate"

    while True:
        try:
            content, finish, body_error = await _call_model(
                provider.client, model, messages, temperature, use_json, timeout, max_tokens, stream,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "AI timeout (provider=%s, model=%s, label=%s, ліміт=%.0fс)", provider.name, model, label, timeout,
            )
            if transient_left > 0:
                transient_left -= 1
                continue
            return None, _SLOW
        except openai.AuthenticationError:
            logger.error(
                "AI провайдер %s: помилка авторизації (невірний/протермінований ключ) — "
                "пропускаю весь провайдер на %.0fс (label=%s)", provider.name, AUTH_ERROR_COOLDOWN_SECONDS, label,
            )
            for m in provider.models:
                _mark_unavailable((provider.name, m), AUTH_ERROR_COOLDOWN_SECONDS)
            for m in _free_ranked:
                _mark_unavailable((provider.name, m), AUTH_ERROR_COOLDOWN_SECONDS)
            _mark_unavailable((provider.name, "openrouter/free"), AUTH_ERROR_COOLDOWN_SECONDS)
            return None, _PROVIDER_DOWN
        except openai.RateLimitError as e:
            cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
            _mark_unavailable(key, cooldown)
            logger.warning(
                "AI 429 (ліміт) на %s/%s, пауза %.0fс, пробую наступну модель (label=%s)",
                provider.name, model, cooldown, label,
            )
            return None, _INSTANT
        except openai.NotFoundError as e:
            _mark_unavailable(key, MODEL_GONE_COOLDOWN_SECONDS)
            logger.warning(
                "AI 404: модель %s/%s недоступна (пауза %.0f год): %s",
                provider.name, model, MODEL_GONE_COOLDOWN_SECONDS / 3600, _short_error(e),
            )
            return None, _INSTANT
        except openai.BadRequestError as e:
            if use_json:
                # Найчастіша причина — модель не приймає response_format=json_object.
                _no_json_support.add(key)
                use_json = False
                logger.info(
                    "%s/%s не підтримує response_format=json_object, повторюю без нього (label=%s)",
                    provider.name, model, label,
                )
                continue
            logger.warning(
                "AI 400 (provider=%s, model=%s, label=%s): %s", provider.name, model, label, _short_error(e),
            )
            return None, _INSTANT
        except openai.APIStatusError as e:
            status = getattr(e, "status_code", 0) or 0
            if status in (402, 403):
                _mark_unavailable(key, ACCESS_ERROR_COOLDOWN_SECONDS)
                logger.warning(
                    "AI %s на %s/%s (немає кредитів/прав?), пауза %.0fс: %s",
                    status, provider.name, model, ACCESS_ERROR_COOLDOWN_SECONDS, _short_error(e),
                )
                return None, _INSTANT
            if status >= 500 or status == 408:
                _mark_unavailable(key, SERVER_ERROR_COOLDOWN_SECONDS)
                logger.warning("AI %s на %s/%s: %s", status, provider.name, model, _short_error(e))
                return None, _INSTANT
            logger.warning("AI HTTP %s (provider=%s, model=%s): %s", status, provider.name, model, _short_error(e))
            return None, _INSTANT
        except Exception:
            logger.exception("AI request failed (provider=%s, model=%s, label=%s)", provider.name, model, label)
            return None, _SLOW

        if body_error:
            logger.warning(
                "AI повернув помилку в тілі відповіді (provider=%s, model=%s, label=%s): %s",
                provider.name, model, label, body_error,
            )
            _mark_unavailable(key, SERVER_ERROR_COOLDOWN_SECONDS)
            return None, _SLOW

        cleaned = _strip_safety_noise((content or "").strip())

        if not cleaned:
            logger.warning(
                "AI повернув ПОРОЖНІЙ content (provider=%s, model=%s, label=%s, finish_reason=%s)",
                provider.name, model, label, finish,
            )
            if use_json:
                # Частина моделей у json_mode віддає порожньо — пробуємо ту саму модель без нього.
                use_json = False
                continue
            if transient_left > 0:
                transient_left -= 1
                continue
            return None, _SLOW

        if expect_json:
            if finish == "length":
                logger.warning(
                    "AI відповідь ОБРІЗАНА (finish_reason=length, provider=%s, model=%s, max_tokens=%s) — "
                    "беру наступну модель", provider.name, model, max_tokens,
                )
                return None, _SLOW
            if _try_parse_json_dict(cleaned) is None:
                logger.warning(
                    "AI повернув не-JSON (provider=%s, model=%s, label=%s). raw[:200]=%r",
                    provider.name, model, label, cleaned[:200],
                )
                return None, _SLOW

        logger.info("AI успішно відповів (provider=%s, model=%s, label=%s)", provider.name, model, label)
        return cleaned, _OK


async def _chat_completion(
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    label: str = "request",
    allow_retry: bool = True,
    expect_json: bool = False,
    needs_vision: bool = False,
) -> str | None:
    """Перебирає провайдерів (основний → резервний) і їхні моделі:
    конфіг → авто-список безкоштовних → openrouter/free. Зупиняється на
    першій успішній відповіді або коли вичерпано ліміт "повільних" відмов."""
    if not _providers:
        return None

    await _maybe_refresh_free_models()

    slow_used = 0
    tried: set[tuple[tuple[str, str], str]] = set()
    limit_reached = False

    for provider in _providers:
        for model in _candidates(provider, needs_vision=needs_vision):
            if slow_used >= AI_MAX_MODELS_PER_REQUEST:
                limit_reached = True
                break
            ident = (provider.identity, model)
            if ident in tried:
                continue
            key = (provider.name, model)
            if _is_unavailable(key):
                logger.info("%s/%s на паузі, пропускаю (label=%s)", provider.name, model, label)
                continue
            tried.add(ident)

            text, outcome = await _run_model(
                provider, model, messages, temperature, json_mode, expect_json, label, allow_retry,
            )
            if outcome == _OK:
                return text
            if outcome == _SLOW:
                slow_used += 1
            elif outcome == _PROVIDER_DOWN:
                break
        if limit_reached:
            break

    logger.error(
        "Усі AI-провайдери/моделі недоступні для запиту (label=%s, спроб з повільною відмовою=%s, провайдери=%s)",
        label, slow_used, [p.name for p in _providers],
    )
    return None


async def _complete(
    prompt: str,
    temperature: float,
    json_mode: bool,
    images: list[str] | None = None,
    allow_retry: bool = True,
    expect_json: bool = False,
) -> str | None:
    content = _build_content(prompt, images)
    messages = [{"role": "user", "content": content}]
    return await _chat_completion(
        messages, temperature, json_mode, label="generate", allow_retry=allow_retry,
        expect_json=expect_json, needs_vision=bool(images),
    )


async def generate_text(prompt: str, temperature: float = 0.6) -> str | None:
    return await _complete(prompt, temperature, json_mode=False)


async def generate_json(prompt: str, temperature: float = 0.7, images: list[str] | None = None) -> dict | None:
    """Кожна модель у ланцюжку перевіряється НА ВАЛІДНИЙ JSON: обрізана
    відповідь або сміття означають перехід до наступної моделі. Тому
    окремий "текстовий" другий прохід більше не потрібен."""
    raw = await _complete(prompt, temperature, json_mode=True, images=images, expect_json=True)
    data = _try_parse_json_dict(raw)
    if data is None:
        logger.error("AI не повернув валідний JSON (усі моделі в ланцюжку відпали).")
    return data


async def chat(messages: list[dict], temperature: float = 0.7) -> str | None:
    return await _chat_completion(messages, temperature, json_mode=False, label="chat")


async def chat_with_tools(messages: list[dict], tools: list[dict], temperature: float = 0.7):
    if not _providers:
        return None

    await _maybe_refresh_free_models()
    tried: set[tuple[tuple[str, str], str]] = set()

    for provider in _providers:
        for model in _candidates(provider, needs_tools=True):
            ident = (provider.identity, model)
            key = (provider.name, model)
            if ident in tried or _is_unavailable(key):
                continue
            tried.add(ident)
            try:
                resp = await asyncio.wait_for(
                    provider.client.chat.completions.create(
                        model=model, messages=messages, tools=tools, tool_choice="auto", temperature=temperature,
                    ),
                    timeout=AI_REQUEST_TIMEOUT_SECONDS,
                )
                choices = getattr(resp, "choices", None)
                if not choices:
                    logger.warning("AI chat_with_tools: порожній choices (provider=%s, model=%s)", provider.name, model)
                    continue
                return choices[0].message
            except asyncio.TimeoutError:
                logger.warning("AI chat_with_tools timeout (provider=%s, model=%s)", provider.name, model)
                continue
            except openai.AuthenticationError:
                logger.error(
                    "AI chat_with_tools: провайдер %s — помилка авторизації, пропускаю провайдер", provider.name,
                )
                for m in provider.models:
                    _mark_unavailable((provider.name, m), AUTH_ERROR_COOLDOWN_SECONDS)
                break
            except openai.RateLimitError as e:
                cooldown = _cooldown_from_error(e) or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                _mark_unavailable(key, cooldown)
                logger.warning("AI chat_with_tools 429 (provider=%s, model=%s)", provider.name, model)
                continue
            except openai.NotFoundError as e:
                _mark_unavailable(key, MODEL_GONE_COOLDOWN_SECONDS)
                logger.warning(
                    "AI chat_with_tools 404: %s/%s недоступна: %s", provider.name, model, _short_error(e),
                )
                continue
            except Exception:
                logger.warning(
                    "Провайдер %s / модель %s не прийняла tools, пробую без них",
                    provider.name, model, exc_info=True,
                )
                try:
                    resp = await asyncio.wait_for(
                        provider.client.chat.completions.create(model=model, messages=messages, temperature=temperature),
                        timeout=AI_REQUEST_TIMEOUT_SECONDS,
                    )
                    choices = getattr(resp, "choices", None)
                    if choices:
                        return choices[0].message
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

    raw = await _chat_completion(
        messages, temperature=0.2, json_mode=True, label="extract_receipt", expect_json=True, needs_vision=True,
    )
    data = _try_parse_json_dict(raw)
    if data is None:
        logger.error("AI не повернув валідний JSON для чека")
    return data


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

    raw = await _chat_completion(
        messages, temperature=0.4, json_mode=True, label="analyze_product_photo",
        expect_json=True, needs_vision=True,
    )
    data = _try_parse_json_dict(raw)
    if data is None:
        logger.error("AI не повернув валідний JSON для товару")
    return data