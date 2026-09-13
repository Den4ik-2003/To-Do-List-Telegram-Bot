"""
services/thread_generator_service.py

Генерація 3 готових Threads-постів під конкретний магазин, у трьох
форматах (провокаційне питання / експертна думка + питання / життєве
дискусійне питання). Тематику магазину визначаємо не лише з назви, а й
з реальних назв товарів (shop_articles) — це чесніше за вигадування
тематики "з голови".

Захист від повторів: перед генерацією в промпт передаються тексти вже
надісланих ідей за останні 30 днів з явною забороною їх повторювати чи
перефразовувати. Додатково після генерації результат звіряється тим самим
методом (SequenceMatcher), що вже використовується в проєкті для дедупу
вакансій (services/jobs_service.py) — якщо збіг занадто високий, робиться
ОДНА повторна спроба генерації (без нескінченних циклів AI-запитів).
"""

import logging
from difflib import SequenceMatcher

from services import ai_service

logger = logging.getLogger("tasks_bot")

VALID_FORMATS = {"provocative", "expert", "discussion"}
FORMAT_LABELS = {
    "provocative": "🔥 Провокаційне питання",
    "expert": "🧠 Експертна думка",
    "discussion": "💬 Дискусійне питання",
}

SIMILARITY_THRESHOLD = 0.75
MAX_PREVIOUS_IN_PROMPT = 10


def _build_prompt(shop_title: str, sample_products: list[str], previous_texts: list[str]) -> str:
    products_text = ", ".join(sample_products[:15]) if sample_products else "невідомо — орієнтуйся на назву магазину"

    previous_block = ""
    if previous_texts:
        recent = previous_texts[:MAX_PREVIOUS_IN_PROMPT]
        bullet_list = "\n".join(f'- "{t}"' for t in recent)
        previous_block = (
            "\n\nВАЖЛИВО: ці пости вже публікувались раніше — НЕ повторюй і НЕ перефразовуй їх, "
            f"придумай щось помітно інше за темою й формулюванням:\n{bullet_list}\n"
        )

    return f"""Ти — SMM-експерт, що готує пости для Threads (текстова соцмережа на кшталт X/Twitter)
для інтернет-магазину.

Магазин: "{shop_title}"
Приклади товарів цього магазину: {products_text}

Згенеруй РІВНО 3 короткі, повністю готові до публікації Threads-пости РІЗНИХ форматів:
1. provocative — 🔥 провокаційне питання: гостра теза або спірна думка, що провокує на дискусію
2. expert — 🧠 експертна думка + питання: коротка експертна думка/інсайт, потім питання
3. discussion — 💬 життєве/дискусійне питання: побутова ситуація, пов'язана з тематикою магазину

Вимоги до КОЖНОГО поста:
- українською мовою;
- короткий, 2-3 речення, до ~280 символів (формат Threads);
- ОБОВ'ЯЗКОВО закінчується сильним відкритим питанням, що заохочує людей коментувати;
- стосується КОНКРЕТНОЇ тематики цього магазину (визнач з назви й прикладів товарів — напр.
  годинники → бренди/стиль/вибір; кросівки → моделі/комфорт/стиль), а НЕ узагальнено про бізнес;
- без хештегів; емодзі — максимум 1, і лише якщо доречно.
{previous_block}
Поверни ЛИШЕ JSON без пояснень і markdown-огорожі:
{{"ideas": [
  {{"format": "provocative", "text": "..."}},
  {{"format": "expert", "text": "..."}},
  {{"format": "discussion", "text": "..."}}
]}}"""


def _too_similar_to_any(ideas: list[dict], previous_texts: list[str]) -> bool:
    for idea in ideas:
        text = (idea.get("text") or "").lower()
        for prev in previous_texts:
            if SequenceMatcher(None, text, prev.lower()).ratio() >= SIMILARITY_THRESHOLD:
                return True
    return False


def _parse_ideas(data: dict | None) -> list[dict] | None:
    if not data or not isinstance(data.get("ideas"), list):
        return None
    ideas = []
    for item in data["ideas"]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        fmt = str(item.get("format") or "").strip().lower()
        if fmt not in VALID_FORMATS:
            fmt = "discussion"
        ideas.append({"format": fmt, "text": text})
    return ideas if len(ideas) >= 3 else None


async def generate_thread_ideas(
    shop_title: str,
    sample_products: list[str],
    previous_texts: list[str],
    _retry: bool = True,
) -> list[dict] | None:
    if not ai_service.is_available():
        return None

    prompt = _build_prompt(shop_title, sample_products, previous_texts)
    data = await ai_service.generate_json(prompt, temperature=0.9)
    ideas = _parse_ideas(data)
    if not ideas:
        return None
    ideas = ideas[:3]

    if previous_texts and _too_similar_to_any(ideas, previous_texts) and _retry:
        logger.info("thread_generator_service: занадто схоже на попередні пости, пробую ще раз для %r", shop_title)
        retried = await generate_thread_ideas(shop_title, sample_products, previous_texts, _retry=False)
        if retried:
            return retried

    return ideas


def format_ideas_message(shop_title: str, ideas: list[dict]) -> str:
    lines = [f"🧵 *Threads-ідеї — {shop_title}*", ""]
    for i, idea in enumerate(ideas, 1):
        label = FORMAT_LABELS.get(idea.get("format"), "🧵")
        lines.append(f"🧵 *Threads #{i}* _({label})_")
        lines.append(idea.get("text", ""))
        lines.append("")
    return "\n".join(lines).strip()