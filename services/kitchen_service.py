"""
НОВИЙ ФАЙЛ: services/kitchen_service.py

Уся AI-логіка фічі 🍳 Кухня. НЕ створює власного AI-клієнта — повністю
використовує вже наявний services/ai_service.py (generate_json/generate_text),
той самий провайдер/модель/ключ, що й решта бота.

Формат рецепту (уніфікований JSON, який повертає AI):
{
    "title": "Карбонара",
    "emoji": "🍝",
    "time_minutes": 25,
    "difficulty": "Середня",   # Легка / Середня / Складна
    "servings": 2,
    "ingredients": ["200 г спагеті", "100 г бекону", ...],
    "steps": ["Відвари спагеті до стану al dente.", ...],
}
"""

import logging

from services import ai_service

logger = logging.getLogger("tasks_bot")

_SAFETY_NOTE = (
    "Обов'язково вказуй потрібну термічну обробку м'яса, риби, яєць там, де це "
    "важливо для безпеки харчування. Не рекомендуй сирі/небезпечні продукти без "
    "явного попередження. Не вигадуй інгредієнти, яких зазвичай немає в цій "
    "страві. Рецепт має бути реальним і практичним."
)

_RECIPE_JSON_SCHEMA = (
    'Поверни ЛИШЕ JSON без пояснень і без markdown-огорожі у форматі:\n'
    '{"title": "назва страви", "emoji": "один емодзі що відповідає страві", '
    '"time_minutes": число_хвилин, '
    '"difficulty": одне з ["Легка","Середня","Складна"], '
    '"servings": число (кількість порцій, якщо не вказано користувачем — 2), '
    '"ingredients": ["кількість + назва інгредієнта", "..."], '
    '"steps": ["крок 1", "крок 2", "..."]}\n'
    "Пиши українською мовою. " + _SAFETY_NOTE
)


def _safe_recipe(data: dict | None) -> dict | None:
    if not data or not data.get("title") or not data.get("steps"):
        return None
    data.setdefault("emoji", "🍽")
    data.setdefault("time_minutes", 30)
    data.setdefault("difficulty", "Середня")
    data.setdefault("servings", 2)
    data.setdefault("ingredients", [])
    if not isinstance(data.get("ingredients"), list):
        data["ingredients"] = []
    if not isinstance(data.get("steps"), list):
        return None
    return data


# ============================================================
# 1. 🍽️ Знайти рецепт (за назвою страви)
# ============================================================

async def generate_recipe(dish_query: str, alt: bool = False) -> dict | None:
    alt_note = (
        " Запропонуй ІНШИЙ варіант рецепту цієї ж страви, помітно відмінний "
        "від класичного (інша техніка, інші додаткові інгредієнти тощо)."
        if alt else ""
    )
    prompt = (
        f'Користувач хоче приготувати: "{dish_query}".{alt_note}\n'
        f"Згенеруй повний зрозумілий рецепт цієї страви.\n{_RECIPE_JSON_SCHEMA}"
    )
    data = await ai_service.generate_json(prompt, temperature=0.6)
    return _safe_recipe(data)


async def generate_recipe_with_context(title: str, context_text: str) -> dict | None:
    """Повний рецепт для страви, обраної зі списку пропозицій (продукти/час/бюджет/продукт)."""
    prompt = (
        f'Згенеруй повний рецепт страви "{title}".\n'
        f"Контекст, який треба врахувати: {context_text}\n{_RECIPE_JSON_SCHEMA}"
    )
    data = await ai_service.generate_json(prompt, temperature=0.6)
    return _safe_recipe(data)


# ============================================================
# 2. 🥕 Що приготувати з продуктів
# ============================================================

async def suggest_from_ingredients(ingredients_text: str) -> list[dict] | None:
    prompt = (
        f'Користувач написав, які продукти має вдома (можливо в довільній, '
        f'розмовній формі): "{ingredients_text}".\n'
        "Проаналізуй цей список і запропонуй 3-5 реальних страв, які можна з "
        "цього приготувати. НЕ вигадуй продукти, яких користувач точно не має, "
        "як основні інгредієнти страви. Базові речі типу сіль/олія/вода можна "
        "вважати наявними за замовчуванням. Для кожної страви коротко вкажи, "
        "чого саме бракує (або порожній рядок, якщо все є).\n"
        'Поверни ЛИШЕ JSON: {"dishes": [{"title": "назва", "emoji": "емодзі", '
        '"have_note": "коротко що з наявного підходить", '
        '"missing_note": "чого бракує, або порожній рядок"}]}'
    )
    data = await ai_service.generate_json(prompt, temperature=0.7)
    if not data or not isinstance(data.get("dishes"), list):
        return None
    return data["dishes"][:5]


# ============================================================
# 3. ⚡ Щось швидке
# ============================================================

async def suggest_quick(minutes: int, extra_text: str = "") -> list[dict] | None:
    extra = f' Додатково користувач уточнив: "{extra_text}".' if extra_text else ""
    prompt = (
        f"Запропонуй 4 страви, які РЕАЛЬНО приготувати максимум за {minutes} "
        f"хвилин (враховуй і час на нарізку/підготовку, і на саме готування)."
        f"{extra}\n"
        'Поверни ЛИШЕ JSON: {"dishes": [{"title": "назва", "emoji": "емодзі"}]}\n'
        "Категорично не пропонуй страви, які фізично неможливо встигнути "
        "приготувати за вказаний час."
    )
    data = await ai_service.generate_json(prompt, temperature=0.7)
    if not data or not isinstance(data.get("dishes"), list):
        return None
    return data["dishes"][:4]


# ============================================================
# 4. 💰 Бюджетна страва
# ============================================================

async def suggest_budget(budget_text: str) -> list[dict] | None:
    prompt = (
        f'Користувач вказав бюджет на страву: "{budget_text}".\n'
        "Запропонуй 4 страви, реальні для цього бюджету. Дай ПРИБЛИЗНУ оцінку "
        "вартості інгредієнтів (це орієнтовна оцінка, не точна ціна — так і "
        "зазнач, якщо не впевнений).\n"
        'Поверни ЛИШЕ JSON: {"dishes": [{"title": "назва", "emoji": "емодзі", '
        '"estimated_cost": "приблизна вартість, напр. \'~90 грн\'"}]}'
    )
    data = await ai_service.generate_json(prompt, temperature=0.7)
    if not data or not isinstance(data.get("dishes"), list):
        return None
    return data["dishes"][:4]


# ============================================================
# 5. 🥩 Рецепт з конкретного продукту
# ============================================================

async def suggest_from_product(product: str) -> list[dict] | None:
    prompt = (
        f'Запропонуй 5 страв, які можна приготувати з "{product}" як основного '
        f"інгредієнта.\n"
        'Поверни ЛИШЕ JSON: {"dishes": [{"title": "назва", "emoji": "емодзі"}]} '
        "(рівно 5 елементів)"
    )
    data = await ai_service.generate_json(prompt, temperature=0.7)
    if not data or not isinstance(data.get("dishes"), list):
        return None
    return data["dishes"][:5]


# ============================================================
# 🔄 Заміна інгредієнта
# ============================================================

async def substitute_ingredient(recipe_title: str, missing_item: str) -> str | None:
    prompt = (
        f'У рецепті "{recipe_title}" немає інгредієнта: "{missing_item}".\n'
        "Запропонуй 2-3 реальні заміни та коротко поясни, як зміниться смак "
        "чи результат. Відповідай простим текстом українською, без "
        "markdown-заголовків, компактно (до 5 речень)."
    )
    return await ai_service.generate_text(prompt, temperature=0.5)


# ============================================================
# 📏 Калькулятор порцій
# ============================================================

async def scale_ingredients(ingredients: list[str], from_servings: int, to_servings: int) -> list[str] | None:
    if from_servings == to_servings:
        return ingredients
    if not ingredients:
        return ingredients
    items_text = "\n".join(f"- {i}" for i in ingredients)
    prompt = (
        f"Ось список інгредієнтів рецепту на {from_servings} порцій:\n{items_text}\n\n"
        f"Перерахуй кількість КОЖНОГО інгредієнта на {to_servings} порцій. "
        "Зберігай одиниці виміру, округлюй до розумних побутових значень "
        "(наприклад, не '0.333 яйця', а розумно округли). Порядок елементів "
        "має ЗБІГАТИСЯ з вхідним списком, і кількість елементів має бути та сама.\n"
        'Поверни ЛИШЕ JSON: {"ingredients": ["перерахований інгредієнт 1", "..."]}'
    )
    data = await ai_service.generate_json(prompt, temperature=0.3)
    if not data or not isinstance(data.get("ingredients"), list):
        return None
    if len(data["ingredients"]) != len(ingredients):
        return None
    return data["ingredients"]