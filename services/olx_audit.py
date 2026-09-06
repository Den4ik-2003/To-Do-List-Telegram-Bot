"""
services/olx_audit.py

AI-логіка фічі "🔍 Аудит мого оголошення". НЕ дублює вже наявний
services/olx_service.fetch_listing_details() — весь парсинг HTML лишається
там, тут тільки AI-аналіз уже зчитаних даних (title/description/price/
params/photos-URLs).

ВАЖЛИВО про фото: vision-аналіз робиться через base64 (як і решта
vision-фіч проєкту — не через прямий image_url, бо не всі AI-провайдери/
моделі однаково коректно тягнуть зображення по сторонньому URL, а
завантажити й закодувати ми можемо самі за допомогою вже наявного
olx_service.fetch_image_bytes()).

ЧЕСНІСТЬ: якщо поточна AI_MODEL не підтримує vision — перший виклик з
фото впаде з помилкою від провайдера. У такому разі функція автоматично
повторює запит БЕЗ фото і явно позначає в результаті, що аналіз
фотографій не проводився, замість того щоб вигадати оцінку.
"""

import base64
import json
import logging
import re

from services import ai_service, olx_service

logger = logging.getLogger("tasks_bot")

# Скільки фото реально відправляємо в AI. Окремо від
# olx_service.MAX_PHOTOS_FOR_AI (=10, використовується для resale-аналізу) —
# тут навмисно менше, бо base64-кодовані повнорозмірні фото суттєво
# збільшують розмір запиту й вартість/час відповіді.
AUDIT_MAX_PHOTOS = 6

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = _JSON_FENCE_RE.sub("", raw.strip())
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
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
                candidate = cleaned[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    logger.exception("olx_audit: AI повернув некоректний JSON: %s", candidate[:300])
                    return None
    return None


async def _download_photos(photo_urls: list[str], limit: int) -> list[dict]:
    downloaded = []
    for url in photo_urls[:limit]:
        result = await olx_service.fetch_image_bytes(url)
        if not result:
            continue
        data, mime = result
        downloaded.append({"mime": mime, "b64": base64.b64encode(data).decode(), "url": url})
    return downloaded


def _build_prompt(listing: dict, photos_downloaded: int) -> str:
    params_text = ", ".join(listing.get("params") or []) or "(недоступно з оголошення)"
    photos_count = listing.get("photos_count") or 0

    photo_instruction = (
        "Фото додані нижче окремими зображеннями — проаналізуй якість, композицію, "
        "освітлення, фон КОЖНОГО фото, і дай загальний висновок по фото."
        if photos_downloaded > 0
        else "Фото недоступні для аналізу (не вдалося завантажити або їх немає в оголошенні) — "
             "оціни ЛИШЕ текстову частину. В полі photo_analysis чесно напиши, що аналіз "
             "фото не проводився, і не вигадуй оцінку якості фото."
    )

    return f"""Ти — експерт з продажу товарів на OLX. Проаналізуй оголошення користувача і дай
чесну, максимально конкретну оцінку з практичними порадами.

Назва: {listing.get('title') or '(немає)'}
Опис: {listing.get('description') or '(немає)'}
Ціна: {listing.get('price')} {listing.get('currency', '')}
Локація: {listing.get('location_text') or '(недоступно з оголошення)'}
Характеристики/параметри: {params_text}
Кількість фото в оголошенні: {photos_count} (проаналізовано: {photos_downloaded})

{photo_instruction}

ОБОВ'ЯЗКОВО:
- Не вигадуй деталі, яких немає в наданих даних (стан товару, комплектацію, категорію
  тощо) — якщо чогось не вказано в оголошенні, так і напиши в issues/actions.
- Ціну оцінюй лише як орієнтовну експертну думку (в тебе немає живих даних конкурентів
  у цьому запиті) — чесно познач це в price_recommendation.reasoning.
- Не давай загальних порад типу "додайте більше фото" без конкретики.

Поверни ЛИШЕ JSON без пояснень і markdown-огорожі:
{{
  "score": число 0-100,
  "issues": ["конкретна проблема 1", "конкретна проблема 2", "..."],
  "actions": ["конкретна дія 1", "конкретна дія 2", "..."],
  "photo_analysis": "1-3 речення аналізу фото, або чесна позначка що аналіз не проводився",
  "new_title": "новий, привабливіший заголовок",
  "new_description": "повністю переписаний, структурований опис",
  "price_recommendation": {{
    "verdict": "конкурентна" | "завищена" | "занижена" | "недостатньо даних",
    "suggested_price": число або null,
    "reasoning": "коротке пояснення"
  }},
  "photos_to_add_or_reshoot": ["конкретна порада по фото 1", "..."]
}}"""


async def _call_vision(messages: list[dict]) -> dict | None:
    if not ai_service.client:
        return None
    try:
        resp = await ai_service.client.chat.completions.create(
            model=ai_service.AI_MODEL if hasattr(ai_service, "AI_MODEL") else None,
            messages=messages,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        return _extract_json(raw)
    except Exception:
        logger.warning("olx_audit: vision-запит не вдався (можливо модель без vision), спробую без фото", exc_info=True)
        return None


async def audit_listing(listing: dict) -> dict | None:
    """
    Повертає dict з ключами score/issues/actions/photo_analysis/new_title/
    new_description/price_recommendation/photos_to_add_or_reshoot, плюс
    службові поля photos_analyzed (int) і vision_used (bool) — щоб
    format_audit_report() міг чесно повідомити, чи фото реально дивились.
    Повертає None при повному провалі AI (і з фото, і без).
    """
    if not ai_service.is_available():
        return None

    photo_urls = listing.get("photos") or []
    downloaded = await _download_photos(photo_urls, AUDIT_MAX_PHOTOS) if photo_urls else []

    if downloaded:
        prompt = _build_prompt(listing, len(downloaded))
        content = [{"type": "text", "text": prompt}]
        for p in downloaded:
            content.append({"type": "image_url", "image_url": {"url": f"data:{p['mime']};base64,{p['b64']}"}})
        result = await _call_vision([{"role": "user", "content": content}])
        if result:
            result["photos_analyzed"] = len(downloaded)
            result["vision_used"] = True
            return result
        logger.info("olx_audit: vision-виклик не дав результату, падаємо назад на текстовий аудит без фото")

    # Фолбек: без фото (або фото взагалі не завантажились/не було)
    text_prompt = _build_prompt(listing, 0)
    text_result = await ai_service.generate_json(text_prompt, temperature=0.4)
    if not text_result:
        return None
    text_result["photos_analyzed"] = 0
    text_result["vision_used"] = False
    return text_result


def format_audit_report(audit: dict, listing: dict) -> str:
    score = audit.get("score", "?")
    issues = audit.get("issues") or []
    actions = audit.get("actions") or []
    photo_analysis = audit.get("photo_analysis") or ""

    lines = [
        "🔍 *Аудит оголошення*",
        "",
        f"📊 Оцінка оголошення: *{score}/100*",
        "",
    ]

    if issues:
        lines.append("❌ *Що не так:*")
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. {issue}")
        lines.append("")

    if actions:
        lines.append("✅ *Що зробити:*")
        for i, action in enumerate(actions, 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    if photo_analysis:
        lines.append(f"🖼 *Фото:* {photo_analysis}")

    if not audit.get("vision_used"):
        lines.append(
            "\n⚠️ _Аналіз фото не проводився_ "
            + ("(немає доступних фото в оголошенні)."
               if not (listing.get("photos") or [])
               else "(не вдалося завантажити фото або поточна AI-модель не підтримує аналіз зображень).")
        )

    return "\n".join(lines).strip()


def format_description(audit: dict) -> str:
    new_title = audit.get("new_title") or "(не згенеровано)"
    new_desc = audit.get("new_description") or "(не згенеровано)"
    return f"✏️ *Новий заголовок:*\n{new_title}\n\n✏️ *Новий опис:*\n{new_desc}"


def format_photo_advice(audit: dict) -> str:
    advice = audit.get("photos_to_add_or_reshoot") or []
    photo_analysis = audit.get("photo_analysis") or ""
    lines = ["📸 *Як покращити фото*", ""]
    if photo_analysis:
        lines.append(photo_analysis)
        lines.append("")
    if advice:
        for i, a in enumerate(advice, 1):
            lines.append(f"{i}. {a}")
    else:
        lines.append("Конкретних порад по фото немає — можливо, аналіз фото не проводився.")
    return "\n".join(lines)


def format_price_advice(audit: dict, listing: dict) -> str:
    rec = audit.get("price_recommendation") or {}
    verdict = rec.get("verdict", "недостатньо даних")
    suggested = rec.get("suggested_price")
    reasoning = rec.get("reasoning", "")
    currency = listing.get("currency", "")
    current_price = listing.get("price")

    lines = [
        "💰 *Оптимальна ціна*", "",
        f"Поточна ціна: {current_price} {currency}",
        f"Вердикт: *{verdict}*",
    ]
    if suggested:
        lines.append(f"Рекомендована ціна: *{suggested:.0f} {currency}*")
    if reasoning:
        lines.append(f"\n💡 {reasoning}")
    lines.append("\n_Це орієнтовна експертна оцінка AI, не результат порівняння з живими конкурентами._")
    return "\n".join(lines)


def format_full_improvement(audit: dict, listing: dict) -> str:
    parts = [
        format_description(audit),
        "",
        format_price_advice(audit, listing),
        "",
        format_photo_advice(audit),
    ]
    return "\n".join(parts)