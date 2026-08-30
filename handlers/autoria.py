import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config.constants import AI_ERROR_TEXT
from database import autoria as autoria_db
from services import autoria_service
from services import ai_service
from keyboards.main_menu import kb_main, kb_cancel
from keyboards.autoria import (
    ikb_autoria_menu, ikb_brands, ikb_models, ikb_year_from, ikb_price,
    ikb_fuel, ikb_gearbox, ikb_result, ikb_ai_confirm,
)
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="autoria")


class AutoriaManual(StatesGroup):
    waiting_brand_text = State()
    waiting_model_text = State()
    waiting_year_from = State()
    waiting_price = State()


class AutoriaAI(StatesGroup):
    waiting_text = State()


_wizard_data: dict[int, dict] = {}


def _label_from_filters(filters: dict) -> str:
    parts = [filters.get("brand_name", ""), filters.get("model_name", "")]
    label = " ".join(p for p in parts if p).strip()
    return label or "Пошук авто"


def _fmt_summary(filters: dict) -> str:
    lines = ["🔎 Параметри пошуку:", ""]
    if filters.get("brand_name"):
        lines.append(f"🚘 Марка: {filters['brand_name']}")
    if filters.get("model_name"):
        lines.append(f"🚘 Модель: {filters['model_name']}")
    if filters.get("year_from"):
        lines.append(f"📅 Рік від: {filters['year_from']}")
    if filters.get("price_to"):
        lines.append(f"💰 Ціна до: ${filters['price_to']:.0f}")
    if filters.get("fuel_name"):
        lines.append(f"⛽ Паливо: {filters['fuel_name']}")
    if filters.get("gearbox_name"):
        lines.append(f"⚙️ КПП: {filters['gearbox_name']}")
    if filters.get("city"):
        lines.append(f"📍 Місто: {filters['city']}")
    return "\n".join(lines)


@router.message(F.text == "🚗 Авто")
async def autoria_menu(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return
    await msg.answer(
        "🚗 *Пошук авто на AUTO.RIA*\n\n"
        "Задай фільтри покроково або опиши, що шукаєш, звичайним текстом — "
        "я сформую готове посилання з фільтрами.",
        reply_markup=ikb_autoria_menu(),
    )


@router.callback_query(F.data == "ar_cancel")
async def ar_cancel_cb(cb: CallbackQuery, state: FSMContext):
    _wizard_data.pop(cb.from_user.id, None)
    await state.clear()
    await cb.answer()
    try:
        await cb.message.edit_text("❌ Пошук скасовано.")
    except Exception:
        pass


@router.callback_query(F.data == "ar_manual")
async def ar_manual_start(cb: CallbackQuery, state: FSMContext):
    _wizard_data[cb.from_user.id] = {}
    await cb.answer()
    await cb.message.edit_text("🚘 *Обери марку:*", reply_markup=ikb_brands())


@router.callback_query(F.data.startswith("ar_brand:"))
async def ar_brand_pick(cb: CallbackQuery, state: FSMContext):
    brand_name = cb.data.split(":", 1)[1]
    await cb.answer("Шукаю...")
    found = await autoria_service.find_brand_id(brand_name)
    if not found:
        return await cb.message.edit_text(
            f"🤔 Не знайшов марку «{brand_name}» в базі AUTO.RIA. Спробуй ще раз:",
            reply_markup=ikb_brands(),
        )
    brand_id, real_name = found
    _wizard_data.setdefault(cb.from_user.id, {})
    _wizard_data[cb.from_user.id]["brand_id"] = brand_id
    _wizard_data[cb.from_user.id]["brand_name"] = real_name

    models = await autoria_service.list_top_models(brand_id)
    if not models:
        _wizard_data[cb.from_user.id]["model_id"] = None
        _wizard_data[cb.from_user.id]["model_name"] = ""
        return await cb.message.edit_text("📅 *Рік від:*", reply_markup=ikb_year_from())

    await cb.message.edit_text(f"🚘 Марка: *{real_name}*\n\nОбери модель:", reply_markup=ikb_models(models))


@router.callback_query(F.data == "ar_brand_other")
async def ar_brand_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AutoriaManual.waiting_brand_text)
    await cb.answer()
    await cb.message.answer("✏️ Напиши марку авто текстом:", reply_markup=kb_cancel())


@router.message(AutoriaManual.waiting_brand_text)
async def ar_brand_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        _wizard_data.pop(msg.from_user.id, None)
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    found = await autoria_service.find_brand_id(msg.text.strip())
    if not found:
        return await msg.answer("🤔 Не знайшов таку марку. Спробуй ще раз або перевір написання:")

    brand_id, real_name = found
    await state.clear()
    _wizard_data.setdefault(msg.from_user.id, {})
    _wizard_data[msg.from_user.id]["brand_id"] = brand_id
    _wizard_data[msg.from_user.id]["brand_name"] = real_name

    models = await autoria_service.list_top_models(brand_id)
    if not models:
        return await msg.answer("📅 *Рік від:*", reply_markup=ikb_year_from())
    await msg.answer(f"🚘 Марка: *{real_name}*\n\nОбери модель:", reply_markup=ikb_models(models))


@router.callback_query(F.data.startswith("ar_model:"))
async def ar_model_pick(cb: CallbackQuery, state: FSMContext):
    _, model_id, model_name = cb.data.split(":", 2)
    _wizard_data.setdefault(cb.from_user.id, {})
    _wizard_data[cb.from_user.id]["model_id"] = int(model_id)
    _wizard_data[cb.from_user.id]["model_name"] = model_name
    await cb.answer()
    await cb.message.edit_text("📅 *Рік від:*", reply_markup=ikb_year_from())


@router.callback_query(F.data == "ar_model_other")
async def ar_model_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AutoriaManual.waiting_model_text)
    await cb.answer()
    await cb.message.answer("✏️ Напиши модель текстом:", reply_markup=kb_cancel())


@router.message(AutoriaManual.waiting_model_text)
async def ar_model_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        _wizard_data.pop(msg.from_user.id, None)
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    wd = _wizard_data.get(msg.from_user.id, {})
    brand_id = wd.get("brand_id")
    if not brand_id:
        await state.clear()
        return await msg.answer("Спочатку обери марку через «🚗 Авто».", reply_markup=kb_main())

    found = await autoria_service.find_model_id(brand_id, msg.text.strip())
    if not found:
        return await msg.answer("🤔 Не знайшов таку модель у цієї марки. Спробуй ще раз:")

    model_id, real_name = found
    await state.clear()
    wd["model_id"] = model_id
    wd["model_name"] = real_name
    await msg.answer("📅 *Рік від:*", reply_markup=ikb_year_from())


@router.callback_query(F.data.startswith("ar_year_from:"))
async def ar_year_from_pick(cb: CallbackQuery, state: FSMContext):
    year = int(cb.data.split(":", 1)[1])
    wd = _wizard_data.setdefault(cb.from_user.id, {})
    if year:
        wd["year_from"] = year
    await cb.answer()
    await cb.message.edit_text("💰 *Максимальна ціна:*", reply_markup=ikb_price())


@router.callback_query(F.data == "ar_year_from_other")
async def ar_year_from_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AutoriaManual.waiting_year_from)
    await cb.answer()
    await cb.message.answer("✏️ Введи рік цифрами (напр. 2016):", reply_markup=kb_cancel())


@router.message(AutoriaManual.waiting_year_from)
async def ar_year_from_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        _wizard_data.pop(msg.from_user.id, None)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    try:
        year = int(msg.text.strip())
    except ValueError:
        return await msg.answer("⚠️ Введи рік цифрами, напр. 2016:")
    wd = _wizard_data.setdefault(msg.from_user.id, {})
    wd["year_from"] = year
    await state.clear()
    await msg.answer("💰 *Максимальна ціна:*", reply_markup=ikb_price())


@router.callback_query(F.data.startswith("ar_price:"))
async def ar_price_pick(cb: CallbackQuery, state: FSMContext):
    price = int(cb.data.split(":", 1)[1])
    wd = _wizard_data.setdefault(cb.from_user.id, {})
    if price:
        wd["price_to"] = price
    await cb.answer()
    await cb.message.edit_text("⛽ *Тип палива:*", reply_markup=ikb_fuel())


@router.callback_query(F.data == "ar_price_other")
async def ar_price_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AutoriaManual.waiting_price)
    await cb.answer()
    await cb.message.answer(
        "✏️ Введи максимальну ціну в доларах (напр. 18000 або 18k):",
        reply_markup=kb_cancel(),
    )


@router.message(AutoriaManual.waiting_price)
async def ar_price_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        _wizard_data.pop(msg.from_user.id, None)
        return await msg.answer("Скасовано.", reply_markup=kb_main())
    price = autoria_service.parse_price_text(msg.text)
    if price is None:
        return await msg.answer("⚠️ Введи суму в доларах, напр. 18000 або 18k:")
    wd = _wizard_data.setdefault(msg.from_user.id, {})
    wd["price_to"] = price
    await state.clear()
    await msg.answer("⛽ *Тип палива:*", reply_markup=ikb_fuel())


@router.callback_query(F.data.startswith("ar_fuel:"))
async def ar_fuel_pick(cb: CallbackQuery, state: FSMContext):
    fuel_id = int(cb.data.split(":", 1)[1])
    wd = _wizard_data.setdefault(cb.from_user.id, {})
    if fuel_id:
        wd["fuel_id"] = fuel_id
        wd["fuel_name"] = {1: "Бензин", 2: "Дизель", 4: "Гібрид", 5: "Електро"}.get(fuel_id, "")
    await cb.answer()
    await cb.message.edit_text("⚙️ *Коробка передач:*", reply_markup=ikb_gearbox())


@router.callback_query(F.data.startswith("ar_gearbox:"))
async def ar_gearbox_pick(cb: CallbackQuery, state: FSMContext):
    gearbox_id = int(cb.data.split(":", 1)[1])
    wd = _wizard_data.setdefault(cb.from_user.id, {})
    if gearbox_id:
        wd["gearbox_id"] = gearbox_id
        wd["gearbox_name"] = {1: "Механіка", 2: "Автомат"}.get(gearbox_id, "")
    await cb.answer()
    await _finish_manual_search(cb.message, cb.from_user.id)


async def _finish_manual_search(target: Message, uid: int):
    filters = _wizard_data.get(uid, {})
    url = autoria_service.build_search_url(filters)

    summary = _fmt_summary(filters)
    text = f"🚗 *AUTO.RIA*\n\n{summary}\n\n🔎 Пошук готовий:"
    try:
        await target.edit_text(text, reply_markup=ikb_result(url))
    except Exception:
        await target.answer(text, reply_markup=ikb_result(url))


@router.callback_query(F.data == "ar_save")
async def ar_save_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    filters = _wizard_data.get(uid)
    if not filters:
        return await cb.answer("Немає активного пошуку для збереження.", show_alert=True)
    await autoria_db.add_saved_search(uid, filters, _label_from_filters(filters))
    await cb.answer("Збережено ⭐")


@router.callback_query(F.data == "ar_list")
async def ar_list_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    searches = await autoria_db.get_user_searches(uid)
    await cb.answer()

    if not searches:
        return await cb.message.answer("📭 У тебе ще немає збережених пошуків.")

    rows = []
    lines = ["⭐ *Твої збережені пошуки:*\n"]
    for s in searches:
        sid = str(s["_id"])
        lines.append(f"• {s.get('label', 'Пошук')}")
        rows.append([
            InlineKeyboardButton(text=f"🔎 {s.get('label', 'Пошук')[:25]}", callback_data=f"ar_open:{sid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ar_del:{sid}"),
        ])

    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ar_open:"))
async def ar_open_cb(cb: CallbackQuery):
    sid = cb.data.split(":", 1)[1]
    searches = await autoria_db.get_user_searches(cb.from_user.id)
    match = next((s for s in searches if str(s["_id"]) == sid), None)
    await cb.answer()
    if not match:
        return await cb.message.answer("⚠️ Пошук не знайдено, можливо його видалено.")

    filters = match["filters"]
    url = autoria_service.build_search_url(filters)
    summary = _fmt_summary(filters)
    await cb.message.answer(f"🚗 *AUTO.RIA*\n\n{summary}\n\n🔎 Пошук готовий:", reply_markup=ikb_result(url))


@router.callback_query(F.data.startswith("ar_del:"))
async def ar_del_cb(cb: CallbackQuery):
    sid = cb.data.split(":", 1)[1]
    ok = await autoria_db.delete_saved_search(sid, cb.from_user.id)
    await cb.answer("Видалено ✅" if ok else "Не знайдено", show_alert=not ok)


AI_PARSE_PROMPT = """Розбери запит користувача про пошук авто на структуровані фільтри.
Поверни ЛИШЕ JSON без пояснень у форматі:
{{"brand": "назва або null", "model": "назва або null", "year_from": число або null,
"price_to_usd": число або null, "fuel": "бензин/дизель/гібрид/електро або null",
"gearbox": "автомат/механіка або null", "city": "назва міста або null"}}

Запит користувача: "{text}"
"""


@router.callback_query(F.data == "ar_ai")
async def ar_ai_start(cb: CallbackQuery, state: FSMContext):
    if not ai_service.is_available():
        await cb.answer()
        return await cb.message.edit_text(AI_ERROR_TEXT)
    await state.set_state(AutoriaAI.waiting_text)
    await cb.answer()
    await cb.message.answer(
        "🧠 Опиши, яке авто шукаєш, звичайним текстом.\n\n"
        "Напр.: «BMW X5 від 2017 до 2021, дизель, автомат, до 25000$, Львів»",
        reply_markup=kb_cancel(),
    )


@router.message(AutoriaAI.waiting_text)
async def ar_ai_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    wait_msg = await msg.answer("🧠 Розбираю запит...")
    parsed = await ai_service.generate_json(AI_PARSE_PROMPT.format(text=msg.text.strip()))
    await state.clear()

    if not parsed:
        return await wait_msg.edit_text(AI_ERROR_TEXT)

    filters = {}
    if parsed.get("brand"):
        found = await autoria_service.find_brand_id(parsed["brand"])
        if found:
            filters["brand_id"], filters["brand_name"] = found
            if parsed.get("model"):
                model_found = await autoria_service.find_model_id(filters["brand_id"], parsed["model"])
                if model_found:
                    filters["model_id"], filters["model_name"] = model_found
    if parsed.get("year_from"):
        try:
            filters["year_from"] = int(parsed["year_from"])
        except (ValueError, TypeError):
            pass
    if parsed.get("price_to_usd"):
        try:
            filters["price_to"] = float(parsed["price_to_usd"])
        except (ValueError, TypeError):
            pass
    fuel_map = {"бензин": 1, "дизель": 2, "гібрид": 4, "електро": 5}
    if parsed.get("fuel") and parsed["fuel"].lower() in fuel_map:
        filters["fuel_id"] = fuel_map[parsed["fuel"].lower()]
        filters["fuel_name"] = parsed["fuel"].capitalize()
    gearbox_map = {"автомат": 2, "механіка": 1}
    if parsed.get("gearbox") and parsed["gearbox"].lower() in gearbox_map:
        filters["gearbox_id"] = gearbox_map[parsed["gearbox"].lower()]
        filters["gearbox_name"] = parsed["gearbox"].capitalize()
    if parsed.get("city"):
        filters["city"] = parsed["city"]

    if not filters:
        return await wait_msg.edit_text(
            "🤔 Не вдалося розпізнати параметри. Спробуй ще раз або скористайся покроковим пошуком."
        )

    _wizard_data[msg.from_user.id] = filters
    summary = _fmt_summary(filters)
    await wait_msg.edit_text(f"🔎 Я зрозумів так:\n\n{summary}", reply_markup=ikb_ai_confirm())


@router.callback_query(F.data == "ar_ai_confirm")
async def ar_ai_confirm_cb(cb: CallbackQuery):
    await cb.answer()
    await _finish_manual_search(cb.message, cb.from_user.id)