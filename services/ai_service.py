"""
ЗМІНЕНИЙ ФАЙЛ: services/ai_service.py

КОРІНЬ ПРОБЛЕМИ "AI повернув некоректний JSON: " (порожній рядок після
двокрапки) видно прямо в traceback: json.decoder.JSONDecodeError:
Expecting value: line 1 column 1 (char 0). Це означає, що AI відповів
HTTP 200 OK, АЛЕ content відповіді був ПОРОЖНІМ рядком. json.loads("")
завжди падає саме так — це не проблема парсингу, це AI віддав пусту
відповідь.

Це типова поведінка деяких reasoning-моделей через OpenRouter в режимі
response_format=json_object: модель "думає" (внутрішній reasoning), і
іноді не встигає/не може сформувати фінальний текстовий content —
провайдер все одно повертає 200 OK з порожнім message.content замість
помилки. Раніше жодного захисту від цього не було: одна порожня
відповідь = мовчазний None → "AI-планувальник тимчасово недоступний"
у ВСІХ розділах бота, що юзають generate_json (кухня, AI-планувальник,
чек, фото товару) — це і пояснює, чому повідомлення "з'являлось часто
в різних розділах".

ВИПРАВЛЕННЯ (застосовано у всіх функціях, що звертаються до AI):
1. Якщо content порожній — робимо ОДИН автоматичний повторний запит
   (з тими самими параметрами) ПЕРЕД тим, як здатися. Порожня відповідь
   здебільшого не повторюється двічі поспіль.
2. Логуємо finish_reason відповіді при порожньому content — це дає
   реальну діагностику (напр. "length" означало б, що reasoning з'їв
   увесь ліміт токенів і треба збільшувати max_tokens — тоді знатимемо
   напевно, а не гадатимемо).
3. Дублікатну логіку виклику chat.completions.create (з fallback без
   response_format) винесено в один спільний хелпер _chat_completion(),
   яким тепер користуються generate_json, extract_receipt та
   analyze_product_photo — щоб цей самий фікс не довелось окремо
   копіювати ще в два місця і не забути десь оновити в майбутньому.

Публічні сигнатури функцій (generate_text/generate_json/chat/
chat_with_tools/extract_receipt/transcribe_voice/analyze_product_photo)
НЕ змінені — усі виклики по всьому проєкту працюють без правок.
"""

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

# Скільки фото максимум передаємо в одному vision-запиті. Обмеження не
# технічне (провайдер прийме й більше), а економічне: кожне фото — це
# суттєва частка вартості запиту, а для resale-аналізу товару 8-10 фото
# з різних ракурсів вже достатньо (services/olx_service.py й так обрізає
# список раніше MAX_PHOTOS_FOR_AI=10, це друга лінія захисту).
MAX_IMAGES_PER_REQUEST = 10

# Скільки разів повторити запит, якщо AI повернув ПОРОЖНІЙ content
# (див. докстрінг файлу вище) — 1 повторна спроба, без нескінченних циклів.
MAX_EMPTY_RETRIES = 1


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


def _build_content(prompt: str, images: list[str] | None):
    """
    Якщо images не передано — повертає звичайний текстовий content (як і
    раніше, щоб не міняти поведінку для всіх існуючих викликів generate_json
    без фото). Якщо images передано — будує мультимодальний content-список
    за тим самим патерном, що вже використовується в analyze_product_photo/
    extract_receipt: [{"type": "text"}, {"type": "image_url"}, ...].

    ВАЖЛИВО: тут передаються прямі http(s) URL фото (як їх віддає OLX), а
    не base64 — OpenAI-сумісні vision-моделі приймають image_url.url як
    звичайне посилання, це не вимагає завантажувати байти фото на боті.
    Якщо конкретний провайдер це не підтримує (не вміє тягнути зовнішні
    URL самостійно) — доведеться перейти на base64, як у analyze_product_photo.
    """
    if not images:
        return prompt

    content = [{"type": "text", "text": prompt}]
    for url in images[:MAX_IMAGES_PER_REQUEST]:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


async def _chat_completion(messages: list[dict], temperature: float, json_mode: bool, label: str = "request") -> str | None:
    """
    Спільний хелпер для одного виклику chat.completions.create з:
    - fallback без response_format, якщо модель його не підтримує;
    - автоматичним retry, якщо AI повернув ПОРОЖНІЙ content (див. докстрінг
      файлу — типова поведінка reasoning-моделей через OpenRouter).

    label — лише для логів (щоб було видно, з якої саме функції прийшла
    порожня відповідь: generate_json / extract_receipt / analyze_product_photo).
    """
    if not client:
        return None

    for attempt in range(MAX_EMPTY_RETRIES + 1):
        try:
            try:
                kwargs = {"temperature": temperature}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(
                    model=AI_MODEL,
                    messages=messages,
                    **kwargs,
                )
            except Exception:
                if not json_mode:
                    raise
                logger.warning("Модель %s не підтримує response_format=json_object, повторюю без нього (%s)", AI_MODEL, label)
                resp = await client.chat.completions.create(
                    model=AI_MODEL,
                    messages=messages,
                    temperature=temperature,
                )
        except Exception:
            logger.exception("AI request failed (model=%s, label=%s)", AI_MODEL, label)
            return None

        choice = resp.choices[0]
        raw = (choice.message.content or "").strip()

        if raw:
            return _strip_safety_noise(raw)

        finish_reason = getattr(choice, "finish_reason", None)
        logger.warning(
            "AI повернув ПОРОЖНІЙ content (model=%s, label=%s, finish_reason=%s, спроба=%s/%s)",
            AI_MODEL, label, finish_reason, attempt + 1, MAX_EMPTY_RETRIES + 1,
        )
        if attempt < MAX_EMPTY_RETRIES:
            continue  # ще одна спроба з тим самим запитом

    return None


async def _complete(prompt: str, temperature: float, json_mode: bool, images: list[str] | None = None) -> str | None:
    content = _build_content(prompt, images)
    messages = [{"role": "user", "content": content}]
    return await _chat_completion(messages, temperature, json_mode, label="generate")


async def generate_text(prompt: str, temperature: float = 0.6) -> str | None:
    return await _complete(prompt, temperature, json_mode=False)


async def generate_json(prompt: str, temperature: float = 0.7, images: list[str] | None = None) -> dict | None:
    """
    ЗМІНЕНО: додано опціональний параметр images (список URL фото) для
    multi-photo vision-аналізу (AI Resale Hunter, services/resale_engine.py).
    Без images поведінка ідентична попередній версії — жоден існуючий
    виклик generate_json(prompt, temperature=...) не ламається.
    """
    raw = await _complete(prompt, temperature, json_mode=True, images=images)
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
    return await _chat_completion(messages, temperature, json_mode=False, label="chat")


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

    raw = await _chat_completion(messages, temperature=0.2, json_mode=True, label="extract_receipt")
    if raw is None:
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

    raw = await _chat_completion(messages, temperature=0.4, json_mode=True, label="analyze_product_photo")
    if raw is None:
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