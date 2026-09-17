"""
ЗМІНЕНИЙ ФАЙЛ: keyboards/jobs.py

Додано клавіатури для флоу "📨 Відгукнутися" (без змін відносно
попередньої версії) ТА, НОВЕ, для майстра створення автопошуку
(handlers/jobs.py AutosearchWizard):
- ikb_autosearch_list_header(): кнопка "➕ Створити автопошук" над
  списком "🔔 Мої монітори вакансій".
- ikb_autosearch_remote() / ikb_autosearch_level(): вибір формату роботи
  і рівня досвіду кнопками (замість вільного тексту).
- ikb_autosearch_confirm(): фінальне підтвердження створення автопошуку.

Решта функцій файлу — без змін.
"""

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
        [InlineKeyboardButton(text="🔗 Відкрити вакансію", url=url)],
        [InlineKeyboardButton(text="🤖 AI аналіз", callback_data=f"jb_analyze:{idx}"), save_btn],
        [InlineKeyboardButton(text="📨 Відгукнутися", callback_data=f"jb_apply_start:{idx}")],
        [InlineKeyboardButton(text="✉️ Cover Letter", callback_data=f"jb_cover:{idx}")],
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


def ikb_empty_search() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Зберегти пошук і чекати нових", callback_data="jb_watch_empty")],
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


# =========================================================
# 📨 Відгукнутися — флоу з двома підтвердженнями
# =========================================================

def ikb_apply_review(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Відправити відгук", callback_data=f"jb_send_confirm:{idx}")],
        [
            InlineKeyboardButton(text="✏️ Змінити Cover Letter", callback_data=f"jb_edit_cover:{idx}"),
            InlineKeyboardButton(text="🔄 Згенерувати заново", callback_data=f"jb_regen_cover:{idx}"),
        ],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"jb_apply_cancel:{idx}")],
    ])


def ikb_apply_final_confirm(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, відправити", callback_data=f"jb_send_final:{idx}")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"jb_apply_cancel:{idx}")],
    ])


def ikb_apply_manual(idx: int, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Відкрити вакансію", url=url)],
        [InlineKeyboardButton(text="✅ Я відгукнувся вручну", callback_data=f"jb_mark_applied:{idx}")],
    ])


# =========================================================
# НОВЕ: 🌙 Автопошук — майстер створення
# =========================================================

def ikb_autosearch_list_header() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Створити автопошук", callback_data="jb_autosearch_new")],
    ])


def ikb_autosearch_remote() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Remote", callback_data="aws_remote:remote")],
        [InlineKeyboardButton(text="🏢 Офіс", callback_data="aws_remote:office")],
        [InlineKeyboardButton(text="🔀 Гібрид", callback_data="aws_remote:hybrid")],
        [InlineKeyboardButton(text="🤷 Не важливо", callback_data="aws_remote:any")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="aws_cancel")],
    ])


def ikb_autosearch_level() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Без досвіду", callback_data="aws_level:no_exp")],
        [InlineKeyboardButton(text="🌱 Junior", callback_data="aws_level:junior")],
        [InlineKeyboardButton(text="🌿 Middle", callback_data="aws_level:middle")],
        [InlineKeyboardButton(text="🌳 Senior", callback_data="aws_level:senior")],
        [InlineKeyboardButton(text="🤷 Будь-який", callback_data="aws_level:any")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="aws_cancel")],
    ])


def ikb_autosearch_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Створити автопошук", callback_data="aws_confirm")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="aws_cancel")],
    ])