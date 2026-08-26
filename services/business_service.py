import logging

from services import ai_service

logger = logging.getLogger("tasks_bot")


async def generate_business_plan(idea_text: str) -> dict | None:
    if not ai_service.is_available():
        return None

    prompt = f"""Ти — практичний бізнес-консультант для України. Користувач дав сиру ідею:
"{idea_text}"

НЕ давай загальних порад типу "створіть Instagram і запустіть рекламу". Дай КОНКРЕТНИЙ
аналіз з реалістичними орієнтовними цифрами в гривнях. Якщо для якогось пункту точних
даних об'єктивно немає (напр. реальні конкуренти) — прямо напиши "недостатньо даних для
точної оцінки", а не вигадуй.

Поверни ЛИШЕ валідний JSON у форматі:
{{
  "idea": "коротке формулювання ідеї",
  "target_client": "хто цільовий клієнт",
  "product": "що конкретно продавати/пропонувати",
  "monetization": "модель заробітку",
  "starting_budget_uah": число,
  "economics": {{"cost_uah": число, "price_uah": число, "profit_uah": число, "margin_percent": число}},
  "sales_channels": ["канал1", "канал2"],
  "competition": "опис конкурентів або 'недостатньо даних для точної оцінки'",
  "market": {{"demand": "опис попиту", "competition_level": "низька|середня|висока",
              "seasonality": "опис сезонності", "potential_margin": "опис"}},
  "risks": ["ризик1", "ризик2"],
  "mvp": "що зробити з мінімальним бюджетом щоб перевірити ідею",
  "plan_7_days": [{{"day": 1, "actions": "що робити"}}],
  "first_money": "реалістичний сценарій перших продажів",
  "scores": {{"potential": число 0-10, "difficulty": число 0-10, "budget_uah": число,
              "risk": число 0-10, "competition": число 0-10, "speed_to_first_money": число 0-10}}
}}

Специфіка товару/послуги (перше тестувати, конкретні артикули/типи, скільки одиниць
протестувати) має бути в полях product, mvp і plan_7_days, а не абстрактною."""

    return await ai_service.generate_json(prompt, temperature=0.5)