from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

STATUS_ORDER = ["saved", "applied", "response", "interview", "hired", "rejected"]
STATUS_LABELS = {
    "saved": "⭐ Збережена",
    "applied": "📨 Відгукнувся",
    "response": "💬 Отримав відповідь",
    "interview": "🎤 Співбесіда",
    "hired": "✅ Прийняли",
    "rejected": "❌ Відмова",
}


def ikb_vacancy_card(idx: int, url: str, saved: bool = False) -> InlineKeyboardMarkup:
    save_btn = InlineKeyboardButton(
        text="✅ Збережено" if saved else "⭐ Зберегти",
        callback_data=f"jb_save:{idx}",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Відкрити", url=url)],
        [InlineKeyboardButton(text="✉️ Cover Letter", callback_data=f"jb_cover:{idx}"), save_btn],
        [InlineKeyboardButton(text="❌ Не показувати такі", callback_data=f"jb_notint:{idx}")],
        [InlineKeyboardButton(text="➡️ Наступна", callback_data="jb_next")],
    ])


def ikb_not_interested_reasons(idx: int) -> InlineKeyboardMarkup:
    reasons = [
        ("💰 Мала зарплата", "salary"),
        ("📍 Не та локація", "location"),
        ("🛠️ Не ті вимоги", "requirements"),
        ("🏢 Не подобається компанія", "company"),
        ("💻 Не той формат", "format"),
        ("❌ Інше", "other"),
    ]
    rows = [[InlineKeyboardButton(text=t, callback_data=f"jb_reason:{idx}:{code}")] for t, code in reasons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_filters_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Зарплата", callback_data="jbf_salary"),
            InlineKeyboardButton(text="📍 Локація", callback_data="jbf_location"),
        ],
        [
            InlineKeyboardButton(text="💻 Remote", callback_data="jbf_remote"),
            InlineKeyboardButton(text="📅 За датою", callback_data="jbf_date"),
        ],
        [InlineKeyboardButton(text="🔄 Скинути фільтри", callback_data="jbf_reset")],
        [InlineKeyboardButton(text="🔔 Стежити за цим пошуком", callback_data="jb_watch")],
    ])


def ikb_search_result_header() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Показати найкращі", callback_data="jb_start_card")],
    ])


def ikb_saved_item(saved_id: str, current_status: str) -> InlineKeyboardMarkup:
    idx = STATUS_ORDER.index(current_status) if current_status in STATUS_ORDER else 0
    next_status = STATUS_ORDER[(idx + 1) % len(STATUS_ORDER)]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"➡️ {STATUS_LABELS[next_status]}",
            callback_data=f"jb_status:{saved_id}:{next_status}",
        )],
        [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"jb_del:{saved_id}")],
    ])


def ikb_watch_item(watch_id: str, active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔕 Вимкнути" if active else "🔔 Увімкнути"
    toggle_cb = f"jbw_off:{watch_id}" if active else f"jbw_on:{watch_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb),
        InlineKeyboardButton(text="🗑 Видалити", callback_data=f"jbw_del:{watch_id}"),
    ]])