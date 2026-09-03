"""
handlers/kitchen.py

ВИПРАВЛЕНО (баг "тап по кнопці Головне меню під час генерації рецепту
сприймається як назва страви"): раніше в кожному waiting_*-хендлері
(kitchen_find_msg, kitchen_from_products_msg, kitchen_budget_msg,
kitchen_single_product_msg, kitchen_substitute_msg) стан FSM скидався
(`await state.set_state(None)`) ТІЛЬКИ ПІСЛЯ того, як AI вже відповів.
Поки йшов AI-запит (від секунд до кількох хвилин — залежить від
завантаженості провайдера), стан лишався в очікуванні тексту. Якщо
користувач у цей час тис будь-яку reply-кнопку меню (напр. "🏠 Головне
меню") — це звичайне текстове повідомлення потрапляло в той самий
хендлер і сприймалось як назва страви/продукту/бюджету, замість того щоб
відпрацювати навігацію. Симптом: другий "🤖 Генерую рецепт..." замість
переходу в головне меню.

Фікс: стан скидається ОДРАЗУ після валідації вхідного тексту, ДО виклику
AI — так наступний тап користувача (навіть якщо AI ще не відповів)
обробляється нормально відповідним хендлером, а не застряє в цьому стані.

Додатково: редагування/видалення "🤖 Генерую..." повідомлення тепер
загорнуте в try/except TelegramAPIError (як і в _render_recipe/
_render_cook_step нижче) — раніше необроблений виняток на цьому кроці
міг залишити повідомлення "Генерую рецепт..." висіти назавжди без жодної
відповіді користувачу.

Решта файлу — без змін відносно оригіналу.
"""

import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config.constants import DB_ERROR_TEXT, AI_ERROR_TEXT
from database.mongo import DBUnavailable
from database import kitchen as kitchen_db
from services import kitchen_service
from keyboards.main_menu import kb_main
from keyboards.kitchen import (
    ikb_kitchen_menu,
    ikb_quick_time,
    ikb_dish_list,
    ikb_recipe_actions,
    ikb_servings,
    ikb_cooking_step,
    ikb_favorites_list,
    ikb_history_list,
    ikb_shopping_list,
    ikb_back_to_kitchen,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="kitchen")


class KitchenStates(StatesGroup):
    waiting_dish = State()
    waiting_ingredients = State()
    waiting_budget = State()
    waiting_single_product = State()
    waiting_substitute = State()


# ============================================================
# ФОРМАТУВАННЯ
# ============================================================

def _fmt_recipe(recipe: dict) -> str:
    emoji = recipe.get("emoji", "🍽")
    title = recipe.get("title", "Рецепт")
    time_m = recipe.get("time_minutes", "?")
    diff = recipe.get("difficulty", "Середня")
    servings = recipe.get("servings", 2)
    ingredients = recipe.get("ingredients", [])
    steps = recipe.get("steps", [])

    lines = [f"{emoji} *{title}*", "", f"⏱ Час: {time_m} хв", f"👨‍🍳 Складність: {diff}", f"🍽 Порції: {servings}", ""]
    lines.append("*🛒 Інгредієнти:*")
    for ing in ingredients:
        lines.append(f"• {ing}")
    lines.append("")
    lines.append("*👨‍🍳 Приготування:*")
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. {step}")
    return "\n".join(lines)


def _fmt_suggestions(dishes: list[dict], header: str) -> str:
    lines = [header, ""]
    for i, d in enumerate(dishes, 1):
        emoji = d.get("emoji") or "🍽"
        lines.append(f"{i}️⃣ *{d.get('title','')}*")
        if d.get("have_note"):
            lines.append(f"✅ {d['have_note']}")
        if d.get("missing_note"):
            lines.append(f"➕ {d['missing_note']}")
        if d.get("estimated_cost"):
            lines.append(f"💰 {d['estimated_cost']}")
        if not any([d.get("have_note"), d.get("missing_note"), d.get("estimated_cost")]):
            lines.append(f"{emoji}")
        lines.append("")
    lines.append("Обери страву нижче, щоб отримати повний рецепт 👇")
    return "\n".join(lines).strip()


async def _render_recipe(target, uid: int, recipe: dict, state: FSMContext, add_to_history: bool = False):
    await state.update_data(current_recipe=recipe, cook_step=0)
    if add_to_history:
        await kitchen_db.add_history(uid, recipe)
    try:
        is_fav = await kitchen_db.is_favorite(uid, recipe.get("title", ""))
    except DBUnavailable:
        is_fav = False
    text = _fmt_recipe(recipe)
    kb = ikb_recipe_actions(is_favorite=is_fav)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        try:
            await target.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            await target.answer(text, reply_markup=kb)


async def _render_cook_step(target, recipe: dict, step: int):
    steps = recipe.get("steps", [])
    total = len(steps)
    if total == 0:
        text = "У цього рецепту немає покрокових інструкцій."
        kb = ikb_back_to_kitchen()
    else:
        step = max(0, min(step, total - 1))
        text = f"👨‍🍳 *Крок {step + 1}/{total}*\n\n{steps[step]}"
        kb = ikb_cooking_step(step, total)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        try:
            await target.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            await target.answer(text, reply_markup=kb)


async def _safe_alert(cb: CallbackQuery, text: str = DB_ERROR_TEXT):
    try:
        await cb.answer(text, show_alert=True)
    except TelegramAPIError:
        pass


async def _safe_edit_or_answer(thinking: Message, text: str, reply_markup=None):
    """
    НОВЕ: безпечне оновлення "думаючого" повідомлення. Раніше прямий виклик
    `thinking.edit_text(...)` без обробки винятку міг залишити повідомлення
    "🤖 Генерую..." висіти назавжди, якщо edit_text з будь-якої причини
    падав (наприклад TelegramAPIError) — виняток просто зупиняв обробку
    без відповіді користувачу.
    """
    try:
        await thinking.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError:
        try:
            await thinking.answer(text, reply_markup=reply_markup)
        except TelegramAPIError:
            logger.exception("kitchen: не вдалося показати результат користувачу (ні edit, ні answer)")


# ============================================================
# ГОЛОВНЕ МЕНЮ КУХНІ
# ============================================================

@router.message(F.text == "🍳 Кухня")
async def kitchen_menu_entry(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await state.clear()
    await msg.answer(
        "🍳 *Кухня*\n\nОбери, що потрібно — і я допоможу з рецептом:",
        reply_markup=ikb_kitchen_menu(),
    )


@router.callback_query(F.data == "kitchen_menu")
async def kitchen_menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    try:
        await cb.message.edit_text("🍳 *Кухня*\n\nОбери, що потрібно:", reply_markup=ikb_kitchen_menu())
    except TelegramAPIError:
        await cb.message.answer("🍳 *Кухня*\n\nОбери, що потрібно:", reply_markup=ikb_kitchen_menu())
    await cb.answer()


@router.callback_query(F.data == "kitchen_exit")
async def kitchen_exit_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("🍳 До зустрічі на кухні! 👋")
    except TelegramAPIError:
        pass
    await cb.answer()


# ============================================================
# 1. 🍽️ ЗНАЙТИ РЕЦЕПТ
# ============================================================

@router.callback_query(F.data == "kitchen_find")
async def kitchen_find_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KitchenStates.waiting_dish)
    try:
        await cb.message.edit_text("🍽️ Напиши, яку страву хочеш приготувати (напр. «карбонара», «борщ», «сирники»):")
    except TelegramAPIError:
        await cb.message.answer("🍽️ Напиши, яку страву хочеш приготувати:")
    await cb.answer()


@router.message(KitchenStates.waiting_dish)
async def kitchen_find_msg(msg: Message, state: FSMContext):
    if not (msg.text or "").strip():
        return await msg.answer("Напиши текстом, яку страву приготувати 🙂")

    dish_query = msg.text.strip()
    # ЗМІНЕНО: стан скидається ТУТ, до запиту в AI — щоб будь-який наступний
    # тап користувача (навіть якщо AI ще не відповів) не сприймався як
    # продовження цього ж діалогу "яку страву приготувати".
    await state.set_state(None)

    thinking = await msg.answer("🤖 Генерую рецепт...")
    recipe = await kitchen_service.generate_recipe(dish_query)
    if not recipe:
        return await _safe_edit_or_answer(thinking, AI_ERROR_TEXT)

    try:
        await thinking.delete()
    except TelegramAPIError:
        pass
    await _render_recipe(msg, msg.from_user.id, recipe, state, add_to_history=True)


# ============================================================
# 2. 🥕 ЩО ПРИГОТУВАТИ З ПРОДУКТІВ
# ============================================================

@router.callback_query(F.data == "kitchen_from_products")
async def kitchen_from_products_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KitchenStates.waiting_ingredients)
    try:
        await cb.message.edit_text("🥕 Напиши, які продукти маєш (можна просто перелічити, як зручно):")
    except TelegramAPIError:
        await cb.message.answer("🥕 Напиши, які продукти маєш:")
    await cb.answer()


@router.message(KitchenStates.waiting_ingredients)
async def kitchen_from_products_msg(msg: Message, state: FSMContext):
    if not (msg.text or "").strip():
        return await msg.answer("Напиши текстом, які продукти маєш 🙂")

    ingredients_text = msg.text.strip()
    await state.set_state(None)  # ЗМІНЕНО: скидання до AI-запиту

    thinking = await msg.answer("🤖 Аналізую продукти...")
    dishes = await kitchen_service.suggest_from_ingredients(ingredients_text)
    if not dishes:
        return await _safe_edit_or_answer(thinking, AI_ERROR_TEXT)

    await state.update_data(suggestions=dishes, suggestion_type="ingredients", suggestion_context=ingredients_text)
    try:
        await thinking.delete()
    except TelegramAPIError:
        pass
    text = _fmt_suggestions(dishes, "🥕 *З твоїх продуктів можна приготувати:*")
    await msg.answer(text, reply_markup=ikb_dish_list(dishes, back_cb="kitchen_menu"))


# ============================================================
# 3. ⚡ ЩОСЬ ШВИДКЕ
# ============================================================

@router.callback_query(F.data == "kitchen_quick")
async def kitchen_quick_cb(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_text("⚡ Скільки максимум часу маєш?", reply_markup=ikb_quick_time())
    except TelegramAPIError:
        await cb.message.answer("⚡ Скільки максимум часу маєш?", reply_markup=ikb_quick_time())
    await cb.answer()


@router.callback_query(F.data.startswith("kitchen_quick_time:"))
async def kitchen_quick_time_cb(cb: CallbackQuery, state: FSMContext):
    try:
        minutes = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    await cb.answer()
    try:
        await cb.message.edit_text("🤖 Підбираю швидкі рецепти...")
    except TelegramAPIError:
        pass
    dishes = await kitchen_service.suggest_quick(minutes)
    if not dishes:
        return await cb.message.edit_text(AI_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())
    await state.update_data(suggestions=dishes, suggestion_type="quick", time_limit=minutes)
    text = _fmt_suggestions(dishes, f"⚡ *Страви до {minutes} хв:*")
    await cb.message.edit_text(text, reply_markup=ikb_dish_list(dishes, back_cb="kitchen_menu"))


# ============================================================
# 4. 💰 БЮДЖЕТНА СТРАВА
# ============================================================

@router.callback_query(F.data == "kitchen_budget")
async def kitchen_budget_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KitchenStates.waiting_budget)
    try:
        await cb.message.edit_text("💰 Напиши бюджет (наприклад «до 100 грн», «до 10€»):")
    except TelegramAPIError:
        await cb.message.answer("💰 Напиши бюджет:")
    await cb.answer()


@router.message(KitchenStates.waiting_budget)
async def kitchen_budget_msg(msg: Message, state: FSMContext):
    if not (msg.text or "").strip():
        return await msg.answer("Напиши бюджет текстом 🙂")

    budget_text = msg.text.strip()
    await state.set_state(None)  # ЗМІНЕНО: скидання до AI-запиту

    thinking = await msg.answer("🤖 Підбираю варіанти...")
    dishes = await kitchen_service.suggest_budget(budget_text)
    if not dishes:
        return await _safe_edit_or_answer(thinking, AI_ERROR_TEXT)

    await state.update_data(suggestions=dishes, suggestion_type="budget", budget_text=budget_text)
    try:
        await thinking.delete()
    except TelegramAPIError:
        pass
    text = _fmt_suggestions(dishes, f"💰 *Страви в межах бюджету «{budget_text}»:*")
    await msg.answer(text, reply_markup=ikb_dish_list(dishes, back_cb="kitchen_menu"))


# ============================================================
# 5. 🥩 РЕЦЕПТ З КОНКРЕТНОГО ПРОДУКТУ
# ============================================================

@router.callback_query(F.data == "kitchen_single_product")
async def kitchen_single_product_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(KitchenStates.waiting_single_product)
    try:
        await cb.message.edit_text("🥩 Напиши продукт, з якого хочеш щось приготувати (напр. «курка»):")
    except TelegramAPIError:
        await cb.message.answer("🥩 Напиши продукт:")
    await cb.answer()


@router.message(KitchenStates.waiting_single_product)
async def kitchen_single_product_msg(msg: Message, state: FSMContext):
    if not (msg.text or "").strip():
        return await msg.answer("Напиши назву продукту текстом 🙂")

    product = msg.text.strip()
    await state.set_state(None)  # ЗМІНЕНО: скидання до AI-запиту

    thinking = await msg.answer("🤖 Підбираю страви...")
    dishes = await kitchen_service.suggest_from_product(product)
    if not dishes:
        return await _safe_edit_or_answer(thinking, AI_ERROR_TEXT)

    await state.update_data(suggestions=dishes, suggestion_type="product", product=product)
    try:
        await thinking.delete()
    except TelegramAPIError:
        pass
    text = _fmt_suggestions(dishes, f"🍗 *Що можна приготувати з «{product}»:*")
    await msg.answer(text, reply_markup=ikb_dish_list(dishes, back_cb="kitchen_menu"))


# ============================================================
# ВИБІР СТРАВИ ЗІ СПИСКУ ПРОПОЗИЦІЙ (спільний для 2, 3, 4, 5)
# ============================================================

@router.callback_query(F.data.startswith("kitchen_pick:"))
async def kitchen_pick_cb(cb: CallbackQuery, state: FSMContext):
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()

    fd = await state.get_data()
    dishes = fd.get("suggestions") or []
    if idx < 0 or idx >= len(dishes):
        return await _safe_alert(cb, "Список застарів, обери ще раз через меню Кухні.")

    title = dishes[idx].get("title", "")
    stype = fd.get("suggestion_type")

    context_parts = []
    if stype == "ingredients":
        context_parts.append(f"Наявні продукти користувача: {fd.get('suggestion_context', '')}.")
    elif stype == "quick":
        context_parts.append(f"Максимум часу на приготування: {fd.get('time_limit')} хв.")
    elif stype == "budget":
        context_parts.append(f"Бюджет: {fd.get('budget_text', '')}.")
    elif stype == "product":
        context_parts.append(f"Основний інгредієнт, який треба використати: {fd.get('product', '')}.")
    context_text = " ".join(context_parts) or "Немає додаткового контексту."

    await cb.answer()
    try:
        await cb.message.edit_text("🤖 Генерую повний рецепт...")
    except TelegramAPIError:
        pass

    recipe = await kitchen_service.generate_recipe_with_context(title, context_text)
    if not recipe:
        return await cb.message.edit_text(AI_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())

    await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=True)


# ============================================================
# ДІЇ НАД РЕЦЕПТОМ
# ============================================================

@router.callback_query(F.data == "kitchen_recipe_regen")
async def kitchen_recipe_regen_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено, спробуй згенерувати новий.")
    await cb.answer()
    try:
        await cb.message.edit_text("🤖 Генерую інший варіант...")
    except TelegramAPIError:
        pass
    new_recipe = await kitchen_service.generate_recipe(recipe.get("title", ""), alt=True)
    if not new_recipe:
        return await cb.message.edit_text(AI_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())
    await _render_recipe(cb.message, cb.from_user.id, new_recipe, state, add_to_history=True)


@router.callback_query(F.data == "kitchen_recipe_fav")
async def kitchen_recipe_fav_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")
    uid = cb.from_user.id
    try:
        already = await kitchen_db.is_favorite(uid, recipe.get("title", ""))
        if already:
            await kitchen_db.remove_favorite_by_title(uid, recipe.get("title", ""))
            await cb.answer("Прибрано з обраного", show_alert=False)
        else:
            await kitchen_db.add_favorite(uid, recipe)
            await cb.answer("Додано в обране ❤️", show_alert=False)
        await _render_recipe(cb.message, uid, recipe, state, add_to_history=False)
    except DBUnavailable:
        await _safe_alert(cb)
    except Exception:
        logger.exception("kitchen_recipe_fav_cb failed for uid=%s", uid)
        await _safe_alert(cb)


@router.callback_query(F.data == "kitchen_recipe_shopping")
async def kitchen_recipe_shopping_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")
    try:
        n = await kitchen_db.add_shopping_items(cb.from_user.id, recipe.get("ingredients", []))
        await cb.answer(f"Додано {n} інгредієнтів у список покупок 🛒", show_alert=True)
    except DBUnavailable:
        await _safe_alert(cb)


@router.callback_query(F.data == "kitchen_recipe_servings")
async def kitchen_recipe_servings_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    if not fd.get("current_recipe"):
        return await _safe_alert(cb, "Рецепт не знайдено.")
    try:
        await cb.message.edit_text("👥 На скільки порцій перерахувати?", reply_markup=ikb_servings())
    except TelegramAPIError:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("kitchen_servings:"))
async def kitchen_servings_pick_cb(cb: CallbackQuery, state: FSMContext):
    try:
        new_servings = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")

    from_servings = recipe.get("servings", 2)
    await cb.answer()
    if from_servings == new_servings:
        return await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=False)

    try:
        await cb.message.edit_text("🤖 Перераховую інгредієнти...")
    except TelegramAPIError:
        pass

    new_ings = await kitchen_service.scale_ingredients(recipe.get("ingredients", []), from_servings, new_servings)
    if new_ings is None:
        return await cb.message.edit_text(AI_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())

    recipe["ingredients"] = new_ings
    recipe["servings"] = new_servings
    await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=False)


@router.callback_query(F.data == "kitchen_recipe_back")
async def kitchen_recipe_back_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")
    await cb.answer()
    await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=False)


@router.callback_query(F.data == "kitchen_recipe_substitute")
async def kitchen_recipe_substitute_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    if not fd.get("current_recipe"):
        return await _safe_alert(cb, "Рецепт не знайдено.")
    await state.set_state(KitchenStates.waiting_substitute)
    try:
        await cb.message.edit_text("🔄 Напиши, якого інгредієнта не вистачає:")
    except TelegramAPIError:
        await cb.message.answer("🔄 Напиши, якого інгредієнта не вистачає:")
    await cb.answer()


@router.message(KitchenStates.waiting_substitute)
async def kitchen_substitute_msg(msg: Message, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        await state.set_state(None)
        return await msg.answer("Рецепт не знайдено, почни з меню Кухні.", reply_markup=kb_main())
    if not (msg.text or "").strip():
        return await msg.answer("Напиши назву інгредієнта текстом 🙂")

    missing_item = msg.text.strip()
    await state.set_state(None)  # ЗМІНЕНО: скидання до AI-запиту

    thinking = await msg.answer("🤖 Підбираю заміну...")
    answer_text = await kitchen_service.substitute_ingredient(recipe.get("title", ""), missing_item)
    if not answer_text:
        return await _safe_edit_or_answer(thinking, AI_ERROR_TEXT)
    await _safe_edit_or_answer(thinking, f"🔄 {answer_text}", reply_markup=ikb_back_to_kitchen())


# ============================================================
# 9. 👨‍🍳 РЕЖИМ "ГОТУЄМО РАЗОМ"
# ============================================================

@router.callback_query(F.data == "kitchen_cook_start")
async def kitchen_cook_start_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено, спробуй ще раз.")
    await state.update_data(cook_step=0)
    await kitchen_db.save_cooking_session(cb.from_user.id, recipe, 0)
    await cb.answer()
    await _render_cook_step(cb.message, recipe, 0)


@router.callback_query(F.data == "kitchen_cook_next")
async def kitchen_cook_next_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Сесію готування не знайдено.")
    step = min(fd.get("cook_step", 0) + 1, max(0, len(recipe.get("steps", [])) - 1))
    await state.update_data(cook_step=step)
    await kitchen_db.save_cooking_session(cb.from_user.id, recipe, step)
    await cb.answer()
    await _render_cook_step(cb.message, recipe, step)


@router.callback_query(F.data == "kitchen_cook_prev")
async def kitchen_cook_prev_cb(cb: CallbackQuery, state: FSMContext):
    fd = await state.get_data()
    recipe = fd.get("current_recipe")
    if not recipe:
        return await _safe_alert(cb, "Сесію готування не знайдено.")
    step = max(fd.get("cook_step", 0) - 1, 0)
    await state.update_data(cook_step=step)
    await kitchen_db.save_cooking_session(cb.from_user.id, recipe, step)
    await cb.answer()
    await _render_cook_step(cb.message, recipe, step)


@router.callback_query(F.data == "kitchen_cook_pause")
async def kitchen_cook_pause_cb(cb: CallbackQuery):
    await cb.answer("⏸ Прогрес збережено. Повертайся, коли будеш готовий(а) 👨‍🍳", show_alert=True)


@router.callback_query(F.data == "kitchen_cook_stop")
async def kitchen_cook_stop_cb(cb: CallbackQuery, state: FSMContext):
    await kitchen_db.clear_cooking_session(cb.from_user.id)
    await cb.answer("Смачного! 😋")
    try:
        await cb.message.edit_text("✅ Готування завершено. Смачного! 😋", reply_markup=ikb_back_to_kitchen())
    except TelegramAPIError:
        await cb.message.answer("✅ Готування завершено. Смачного! 😋", reply_markup=ikb_back_to_kitchen())


# ============================================================
# 6. ❤️ ОБРАНЕ
# ============================================================

@router.callback_query(F.data == "kitchen_favorites")
async def kitchen_favorites_cb(cb: CallbackQuery):
    try:
        favorites = await kitchen_db.get_favorites(cb.from_user.id)
    except DBUnavailable:
        return await cb.message.edit_text(DB_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())
    await cb.answer()
    if not favorites:
        return await cb.message.edit_text("❤️ Поки немає обраних рецептів.", reply_markup=ikb_back_to_kitchen())
    await cb.message.edit_text("❤️ *Обрані рецепти:*", reply_markup=ikb_favorites_list(favorites))


@router.callback_query(F.data.startswith("kitchen_fav_open:"))
async def kitchen_fav_open_cb(cb: CallbackQuery, state: FSMContext):
    try:
        rid = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    try:
        recipe = await kitchen_db.get_favorite(cb.from_user.id, rid)
    except DBUnavailable:
        return await _safe_alert(cb)
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")
    await cb.answer()
    await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=False)


@router.callback_query(F.data.startswith("kitchen_fav_del:"))
async def kitchen_fav_del_cb(cb: CallbackQuery):
    try:
        rid = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    try:
        await kitchen_db.remove_favorite(cb.from_user.id, rid)
        favorites = await kitchen_db.get_favorites(cb.from_user.id)
    except DBUnavailable:
        return await _safe_alert(cb)
    await cb.answer("Видалено")
    if not favorites:
        return await cb.message.edit_text("❤️ Поки немає обраних рецептів.", reply_markup=ikb_back_to_kitchen())
    await cb.message.edit_text("❤️ *Обрані рецепти:*", reply_markup=ikb_favorites_list(favorites))


# ============================================================
# 7. 📜 ІСТОРІЯ
# ============================================================

@router.callback_query(F.data == "kitchen_history")
async def kitchen_history_cb(cb: CallbackQuery):
    try:
        history = await kitchen_db.get_history(cb.from_user.id)
    except DBUnavailable:
        return await cb.message.edit_text(DB_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())
    await cb.answer()
    if not history:
        return await cb.message.edit_text("📜 Історія рецептів поки порожня.", reply_markup=ikb_back_to_kitchen())
    history_str_ids = [{**h, "_id": str(h["_id"])} for h in history]
    await cb.message.edit_text("📜 *Останні рецепти:*", reply_markup=ikb_history_list(history_str_ids))


@router.callback_query(F.data.startswith("kitchen_hist_open:"))
async def kitchen_hist_open_cb(cb: CallbackQuery, state: FSMContext):
    hid = cb.data.split(":", 1)[1]
    try:
        recipe = await kitchen_db.get_history_item(cb.from_user.id, hid)
    except DBUnavailable:
        return await _safe_alert(cb)
    if not recipe:
        return await _safe_alert(cb, "Рецепт не знайдено.")
    await cb.answer()
    await _render_recipe(cb.message, cb.from_user.id, recipe, state, add_to_history=False)


# ============================================================
# 8. 🛒 СПИСОК ПОКУПОК
# ============================================================

@router.callback_query(F.data == "kitchen_shop_open")
async def kitchen_shop_open_cb(cb: CallbackQuery):
    try:
        items = await kitchen_db.get_shopping_items(cb.from_user.id)
    except DBUnavailable:
        return await cb.message.edit_text(DB_ERROR_TEXT, reply_markup=ikb_back_to_kitchen())
    await cb.answer()
    items_str_ids = [{**i, "_id": str(i["_id"])} for i in items]
    text = "🛒 *Список покупок*" if items else "🛒 Список покупок поки порожній.\n\nДодавай інгредієнти прямо з рецепту кнопкою «🛒 Список покупок»."
    await cb.message.edit_text(text, reply_markup=ikb_shopping_list(items_str_ids))


@router.callback_query(F.data.startswith("kitchen_shop_toggle:"))
async def kitchen_shop_toggle_cb(cb: CallbackQuery):
    item_id = cb.data.split(":", 1)[1]
    try:
        await kitchen_db.toggle_shopping_item(cb.from_user.id, item_id)
        items = await kitchen_db.get_shopping_items(cb.from_user.id)
    except DBUnavailable:
        return await _safe_alert(cb)
    await cb.answer()
    items_str_ids = [{**i, "_id": str(i["_id"])} for i in items]
    text = "🛒 *Список покупок*" if items else "🛒 Список покупок поки порожній."
    await cb.message.edit_text(text, reply_markup=ikb_shopping_list(items_str_ids))


@router.callback_query(F.data == "kitchen_shop_clear")
async def kitchen_shop_clear_cb(cb: CallbackQuery):
    try:
        await kitchen_db.clear_checked_shopping_items(cb.from_user.id)
        items = await kitchen_db.get_shopping_items(cb.from_user.id)
    except DBUnavailable:
        return await _safe_alert(cb)
    await cb.answer("Прибрано куплене")
    items_str_ids = [{**i, "_id": str(i["_id"])} for i in items]
    text = "🛒 *Список покупок*" if items else "🛒 Список покупок поки порожній."
    await cb.message.edit_text(text, reply_markup=ikb_shopping_list(items_str_ids))