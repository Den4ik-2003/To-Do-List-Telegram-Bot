import logging
from datetime import date, datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database import users as users_db
from services import weather_service
from keyboards.main_menu import kb_main, kb_cancel
from handlers.common import require_auth

logger = logging.getLogger("tasks_bot")
router = Router(name="weather")

# Тимчасовий кеш кандидатів міст на час вибору (per uid), щоб не пхати
# великий JSON у callback_data (там ліміт 64 байти)
_pending_options: dict[int, list[dict]] = {}


class WeatherCity(StatesGroup):
    waiting_city = State()


class WeatherDate(StatesGroup):
    waiting_date = State()


def _ikb_city_options(options: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([InlineKeyboardButton(
            text=weather_service.format_option_label(opt),
            callback_data=f"weather_pick:{i}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="weather_pick_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ikb_change_city() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Погодинно на день", callback_data="weather_hourly")],
        [InlineKeyboardButton(text="🌍 Змінити місто/країну", callback_data="weather_change_city")],
    ])


def _ikb_date_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сьогодні", callback_data="weather_date:0"),
            InlineKeyboardButton(text="Завтра", callback_data="weather_date:1"),
        ],
        [InlineKeyboardButton(text="✏️ Ввести іншу дату", callback_data="weather_date_custom")],
    ])


async def _ask_city(msg: Message, state: FSMContext, prompt: str):
    await state.set_state(WeatherCity.waiting_city)
    await msg.answer(prompt, reply_markup=kb_cancel())


@router.message(F.text == "🌤️ Погода")
async def weather_show(msg: Message, state: FSMContext):
    if not await require_auth(msg, state):
        return

    user_state = await users_db.get_user_state(msg.from_user.id)
    lat = user_state.get("city_lat")
    lon = user_state.get("city_lon")
    display_name = user_state.get("city_display")

    if lat is None or lon is None:
        return await _ask_city(
            msg, state,
            "🌤️ *Погода*\n\nНапиши своє місто (можна разом з країною через кому, "
            "напр. `Львів` або `Paris, France`), щоб я міг показувати погоду й "
            "ранкові підказки щодо одягу.",
        )

    wait_msg = await msg.answer("🌤️ Дивлюсь погоду...")
    report = await weather_service.build_weather_report(lat, lon, display_name)
    if not report:
        return await wait_msg.edit_text("⚠️ Не вдалося отримати погоду. Спробуй пізніше.")
    await wait_msg.edit_text(report, reply_markup=_ikb_change_city())


@router.callback_query(F.data == "weather_change_city")
async def weather_change_city_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_city(
        cb.message, state,
        "🌍 Напиши нове місто (можна з країною через кому, напр. `Кельн, Німеччина`):",
    )


@router.callback_query(F.data == "weather_hourly")
async def weather_hourly_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer("На яку дату показати погодинний прогноз?", reply_markup=_ikb_date_choice())


@router.callback_query(F.data.startswith("weather_date:"))
async def weather_date_pick_cb(cb: CallbackQuery, state: FSMContext):
    days_offset = int(cb.data.split(":", 1)[1])
    target_date = date.today() + timedelta(days=days_offset)
    await cb.answer()
    await _send_hourly_day(cb.message, cb.from_user.id, target_date)


@router.callback_query(F.data == "weather_date_custom")
async def weather_date_custom_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(WeatherDate.waiting_date)
    await cb.message.answer(
        "✏️ Напиши дату у форматі `ДД.ММ` або `ДД.ММ.РРРР` (напр. `25.12`):",
        reply_markup=kb_cancel(),
    )


@router.message(WeatherDate.waiting_date)
async def weather_date_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    raw = msg.text.strip()
    target_date = None
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt)
            year = parsed.year if fmt == "%d.%m.%Y" else date.today().year
            candidate = date(year, parsed.month, parsed.day)
            if fmt == "%d.%m" and candidate < date.today():
                candidate = date(year + 1, parsed.month, parsed.day)
            target_date = candidate
            break
        except ValueError:
            continue

    if target_date is None:
        return await msg.answer("🤔 Не зрозумів дату. Формат: `ДД.ММ` або `ДД.ММ.РРРР`, напр. `25.12`.")

    await state.clear()
    await _send_hourly_day(msg, msg.from_user.id, target_date)


async def _send_hourly_day(target: Message, uid: int, target_date: date):
    if target_date < date.today():
        return await target.answer("⚠️ Прогноз доступний лише на сьогодні й наперед.")

    user_state = await users_db.get_user_state(uid)
    lat = user_state.get("city_lat")
    lon = user_state.get("city_lon")
    display_name = user_state.get("city_display")

    if lat is None or lon is None:
        return await target.answer(
            "⚠️ Спершу вкажи місто через «🌤️ Погода».",
            reply_markup=kb_main(),
        )

    wait_msg = await target.answer("📅 Формую погодинний прогноз...")
    report = await weather_service.build_hourly_day_report(lat, lon, display_name, target_date)
    if not report:
        return await wait_msg.edit_text(
            "⚠️ Не вдалося отримати погодинний прогноз на цю дату. "
            "Можливо, вона задалеко в майбутньому (доступно до 16 днів)."
        )
    await wait_msg.edit_text(report, reply_markup=_ikb_change_city())


@router.message(WeatherCity.waiting_city)
async def weather_set_city(msg: Message, state: FSMContext):
    if msg.text == "❌ Скасувати":
        await state.clear()
        return await msg.answer("Скасовано.", reply_markup=kb_main())

    query = (msg.text or "").strip()
    if not query:
        return await msg.answer("Напиши, будь ласка, назву міста текстом.")

    options = await weather_service.search_city_options(query)

    if not options:
        return await msg.answer(
            "🤔 Не знайшов таке місто. Перевір написання або уточни країну — "
            "напр. `Одеса, Україна`."
        )

    if len(options) == 1:
        await _apply_city(msg.from_user.id, options[0], msg, state)
        return

    # Кілька варіантів — показуємо на вибір, щоб уникнути помилки з
    # однойменними містами в різних країнах
    _pending_options[msg.from_user.id] = options
    names_preview = "\n".join(f"• {weather_service.format_option_label(o)}" for o in options)
    await msg.answer(
        f"🔎 Знайшов кілька варіантів для «{query}»:\n\n{names_preview}\n\nОбери потрібний:",
        reply_markup=_ikb_city_options(options),
    )


@router.callback_query(F.data.startswith("weather_pick:"))
async def weather_pick_cb(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    options = _pending_options.get(uid)
    if not options:
        await cb.answer()
        return await cb.message.edit_text("⚠️ Варіанти застаріли, спробуй ще раз.")

    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(options):
        await cb.answer()
        return

    chosen = options[idx]
    _pending_options.pop(uid, None)
    await cb.answer()
    await _apply_city(uid, chosen, cb.message, state, edit=True)


@router.callback_query(F.data == "weather_pick_cancel")
async def weather_pick_cancel_cb(cb: CallbackQuery, state: FSMContext):
    _pending_options.pop(cb.from_user.id, None)
    await state.clear()
    await cb.answer()
    await cb.message.edit_text("Скасовано.")


async def _apply_city(uid: int, opt: dict, target: Message, state: FSMContext, edit: bool = False):
    display_name = weather_service.format_option_label(opt)
    await users_db.save_user_state(uid, {
        "city_lat": opt["lat"],
        "city_lon": opt["lon"],
        "city_display": display_name,
    })
    await state.clear()

    text = f"✅ Місто збережено: {display_name}\n\n🌤️ Дивлюсь погоду..."
    if edit:
        await target.edit_text(text)
    else:
        await target.answer(text, reply_markup=kb_main())

    report = await weather_service.build_weather_report(opt["lat"], opt["lon"], display_name)
    if report:
        if edit:
            await target.answer(report, reply_markup=_ikb_change_city())
        else:
            await target.answer(report, reply_markup=_ikb_change_city())