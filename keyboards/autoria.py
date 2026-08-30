from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

POPULAR_BRANDS = ["BMW", "Audi", "Mercedes-Benz", "Volkswagen", "Toyota", "Skoda"]


def ikb_autoria_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Пошук за фільтрами", callback_data="ar_manual")],
        [InlineKeyboardButton(text="🧠 AI пошук авто", callback_data="ar_ai")],
        [InlineKeyboardButton(text="⭐ Мої пошуки", callback_data="ar_list")],
    ])


def ikb_brands() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=b, callback_data=f"ar_brand:{b}")] for b in POPULAR_BRANDS]
    rows.append([InlineKeyboardButton(text="✏️ Інша марка (ввести текстом)", callback_data="ar_brand_other")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="ar_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_models(models: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=m["name"], callback_data=f"ar_model:{m['value']}:{m['name']}")] for m in models]
    rows.append([InlineKeyboardButton(text="✏️ Інша модель (ввести текстом)", callback_data="ar_model_other")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ar_manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_year_from() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="від 2015", callback_data="ar_year_from:2015"),
            InlineKeyboardButton(text="від 2018", callback_data="ar_year_from:2018"),
            InlineKeyboardButton(text="від 2020", callback_data="ar_year_from:2020"),
        ],
        [InlineKeyboardButton(text="✏️ Свій рік", callback_data="ar_year_from_other")],
        [InlineKeyboardButton(text="➡️ Пропустити", callback_data="ar_year_from:0")],
    ])


def ikb_price() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="до $5 000", callback_data="ar_price:5000"),
            InlineKeyboardButton(text="до $10 000", callback_data="ar_price:10000"),
        ],
        [
            InlineKeyboardButton(text="до $15 000", callback_data="ar_price:15000"),
            InlineKeyboardButton(text="до $25 000", callback_data="ar_price:25000"),
        ],
        [InlineKeyboardButton(text="✏️ Своя сума", callback_data="ar_price_other")],
        [InlineKeyboardButton(text="➡️ Пропустити", callback_data="ar_price:0")],
    ])


def ikb_fuel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⛽ Бензин", callback_data="ar_fuel:1"),
            InlineKeyboardButton(text="⛽ Дизель", callback_data="ar_fuel:2"),
        ],
        [
            InlineKeyboardButton(text="🔋 Гібрид", callback_data="ar_fuel:4"),
            InlineKeyboardButton(text="🔌 Електро", callback_data="ar_fuel:5"),
        ],
        [InlineKeyboardButton(text="➡️ Будь-яке", callback_data="ar_fuel:0")],
    ])


def ikb_gearbox() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚙️ Автомат", callback_data="ar_gearbox:2"),
            InlineKeyboardButton(text="⚙️ Механіка", callback_data="ar_gearbox:1"),
        ],
        [InlineKeyboardButton(text="➡️ Будь-яка", callback_data="ar_gearbox:0")],
    ])


def ikb_result(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 ВІДКРИТИ AUTO.RIA", url=url)],
        [InlineKeyboardButton(text="⭐ Зберегти пошук", callback_data="ar_save")],
    ])


def ikb_ai_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Підтвердити", callback_data="ar_ai_confirm")],
        [InlineKeyboardButton(text="✏️ Написати інакше", callback_data="ar_ai")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="ar_cancel")],
    ])