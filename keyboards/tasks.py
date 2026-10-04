from datetime import date

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config.constants import LABELS, CATEGORIES, STATUS_DONE
from utils.dates import is_missed, WEEKDAYS_UA


def kb_tasks_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Додати задачу"), KeyboardButton(text="🎙 Голосова задача")],
        [KeyboardButton(text="📋 Сьогодні"), KeyboardButton(text="📅 Майбутні")],
        [KeyboardButton(text="✅ Виконані"), KeyboardButton(text="⭐ Обране")],
        [KeyboardButton(text="🏷 Категорії"), KeyboardButton(text="🔁 Повторювані таски")],
        [KeyboardButton(text="◀️ Головне меню")],
    ], resize_keyboard=True)


def kb_label() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔴 Терміново"), KeyboardButton(text="🟡 Середньо")],
        [KeyboardButton(text="🟢 Не поспішає"), KeyboardButton(text="🔵 Ідея")],
        [KeyboardButton(text="🟣 Особисте")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def label_from_text(text: str) -> str | None:
    mapping = {
        "🔴 Терміново": "urgent", "🟡 Середньо": "medium", "🟢 Не поспішає": "low",
        "🔵 Ідея": "idea", "🟣 Особисте": "personal",
    }
    return mapping.get(text)


def kb_category() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💻 Робота"), KeyboardButton(text="💰 Фінанси")],
        [KeyboardButton(text="🏠 Дім"), KeyboardButton(text="💪 Спорт")],
        [KeyboardButton(text="📚 Навчання"), KeyboardButton(text="💼 Бізнес")],
        [KeyboardButton(text="💡 Ідея"), KeyboardButton(text="🗂 Інше")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def category_from_text(text: str) -> str | None:
    mapping = {
        "💻 Робота": "work", "💰 Фінанси": "finance", "🏠 Дім": "home",
        "💪 Спорт": "sport", "📚 Навчання": "study", "💼 Бізнес": "business",
        "💡 Ідея": "idea", "🗂 Інше": "other",
    }
    return mapping.get(text)


def kb_project_select(projects: list) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="📋 Без проекту")]]
    for p in projects:
        rows.append([KeyboardButton(text=f"📁 {p.get('title','')}"[:64])])
    rows.append([KeyboardButton(text="❌ Скасувати")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def project_from_text(text: str, projects: list) -> dict | None:
    if not text or not text.startswith("📁 "):
        return None
    title = text[2:].strip()
    for p in projects:
        if p.get("title", "")[:64] == title:
            return p
    return None


def kb_date() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Сьогодні"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="✏️ Своя дата (дд.мм.рррр)")],
        [KeyboardButton(text="⏭ Без терміну")],
        [KeyboardButton(text="❌ Скасувати")],
    ], resize_keyboard=True)


def ikb_task_actions(tid: int, t: dict) -> InlineKeyboardMarkup:
    if t.get("pinned"):
        pin_btn = InlineKeyboardButton(text="📌 Відкріпити", callback_data=f"unpin:{tid}")
    else:
        pin_btn = InlineKeyboardButton(text="⭐ Закріпити", callback_data=f"pin:{tid}")
    rows = [
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [pin_btn, InlineKeyboardButton(text="📝 Підзадачі", callback_data=f"subtasks:{tid}")],
        [
            InlineKeyboardButton(text="🕐 +1 год", callback_data=f"postp1h:{tid}"),
            InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}"),
        ],
        [InlineKeyboardButton(text="📅 Найближчий вільний", callback_data=f"autoresched:{tid}")],
        [
            InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"edit:{tid}"),
            InlineKeyboardButton(text="🗑 Видалити", callback_data=f"deltask:{tid}"),
        ],
    ]
    rid = t.get("recurring_task_id")
    if rid:
        rows.append([
            InlineKeyboardButton(text="❌ Не сьогодні", callback_data=f"rcskip:{tid}"),
            InlineKeyboardButton(text="🗑 Видалити повторення", callback_data=f"rcd:{rid}"),
        ])
        rows.append([InlineKeyboardButton(text="🔁 Шаблон повторення", callback_data=f"rcv:{rid}")])
    else:
        rows.append([InlineKeyboardButton(text="🔁 Зробити повторюваною", callback_data=f"rcsch_open:t{tid}")])
    rows.append([InlineKeyboardButton(text="◀️ До списку", callback_data="back_to_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_rollover_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
        [InlineKeyboardButton(text="📅 Найближчий вільний", callback_data=f"autoresched:{tid}")],
        [
            InlineKeyboardButton(text="🕐 Через 1 год", callback_data=f"postp1h:{tid}"),
            InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}"),
        ],
        [
            InlineKeyboardButton(text="🔕 Залишити", callback_data=f"dismiss_rollover:{tid}"),
            InlineKeyboardButton(text="❌ Видалити", callback_data=f"deltask:{tid}"),
        ],
    ])


def ikb_rollover_digest() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Перенести всі", callback_data="resched_all")],
        [InlineKeyboardButton(text="⚙️ Вибрати задачі", callback_data="resched_pick")],
        [InlineKeyboardButton(text="❌ Залишити", callback_data="dismiss_all")],
    ])


def ikb_view_day(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Переглянути день", callback_data=f"viewday:{day.isoformat()}")],
    ])


def ikb_reminder_actions(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}")],
    ])


def ikb_edit_fields(tid: int) -> InlineKeyboardMarkup:
    fields = [
        ("text", "📝 Текст"),
        ("label", "🎨 Мітка"),
        ("category", "🏷 Категорія"),
        ("date", "📅 Дата"),
        ("time", "🕐 Час"),
        ("subtasks_add", "➕ Додати підзадачі"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"editfield:{tid}:{key}")] for key, label in fields]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"view:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_tasks_list(tasks: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = tasks[start:start + per_page]
    rows = []
    for t in chunk:
        label = LABELS.get(t.get("label", "idea"), {"emoji": ""})
        status_icon = "✅" if t.get("status") == STATUS_DONE else ("⚠️" if is_missed(t) else "⏳")
        pin_str = "📌" if t.get("pinned") else ""
        due_short = t.get("due", "")[-5:] if t.get("due") else "без терм."
        lbl = f"{status_icon} {pin_str}{label['emoji']} №{t['id']} {due_short} {t.get('text','')[:18]}"
        rows.append([InlineKeyboardButton(text=lbl, callback_data=f"view:{t['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page-1}"))
    total_pages = max(1, (len(tasks) - 1) // per_page + 1)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if start + per_page < len(tasks):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_categories() -> InlineKeyboardMarkup:
    rows = []
    for key, cat in CATEGORIES.items():
        rows.append([InlineKeyboardButton(text=f"{cat['emoji']} {cat['name']}", callback_data=f"catopen:{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tasks_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_one_thing_actions(done: bool = False) -> InlineKeyboardMarkup:
    if done:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обрати іншу", callback_data="onething_reroll")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зроблено", callback_data="onething_done")],
        [InlineKeyboardButton(text="🔄 Обрати іншу", callback_data="onething_reroll")],
    ])


def ikb_recurring_notice(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{tid}"),
            InlineKeyboardButton(text="➡️ Перенести", callback_data=f"rcmv:{tid}"),
        ],
        [
            InlineKeyboardButton(text="❌ Пропустити", callback_data=f"rcskip:{tid}"),
            InlineKeyboardButton(text="✏️ Змінити", callback_data=f"edit:{tid}"),
        ],
    ])


def ikb_recurring_move(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🕐 +1 год", callback_data=f"postp1h:{tid}"),
            InlineKeyboardButton(text="📅 Завтра", callback_data=f"postptom:{tid}"),
        ],
        [InlineKeyboardButton(text="🗓 Обрати дату", callback_data=f"editfield:{tid}:date")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"rcback:{tid}")],
    ])


def ikb_recurring_suggestion(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Так, щодня", callback_data=f"rcsy:{sid}:daily"),
            InlineKeyboardButton(text="Так, по буднях", callback_data=f"rcsy:{sid}:weekdays"),
        ],
        [InlineKeyboardButton(text="Обрати графік", callback_data=f"rcsp:{sid}")],
        [
            InlineKeyboardButton(text="Ні", callback_data=f"rcsn:{sid}"),
            InlineKeyboardButton(text="Не пропонувати знову", callback_data=f"rcsx:{sid}"),
        ],
    ])


def ikb_rc_schedule(ctx: str) -> InlineKeyboardMarkup:
    layout = [
        [("Щодня", "daily"), ("По буднях", "weekdays")],
        [("По вихідних", "weekends"), ("Щотижня", "weekly")],
        [("Кожні 2 тижні", "biweekly"), ("Щомісяця", "monthly")],
        [("Кожні N днів", "every_n"), ("Дні тижня", "days")],
        [("Число місяця", "monthday")],
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"rcsch:{ctx}:{kind}") for label, kind in row]
        for row in layout
    ]
    rows.append([InlineKeyboardButton(text="◀️ Скасувати", callback_data=f"rcsch_x:{ctx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_rc_days(selected) -> InlineKeyboardMarkup:
    sel = set(selected)
    btns = [
        InlineKeyboardButton(text=("✅ " if i in sel else "") + WEEKDAYS_UA[i], callback_data=f"rcdw:{i}")
        for i in range(7)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        btns[:4],
        btns[4:],
        [
            InlineKeyboardButton(text="✔️ Готово", callback_data="rcdw_ok"),
            InlineKeyboardButton(text="◀️ Скасувати", callback_data="rcdw_cancel"),
        ],
    ])


def ikb_recurring_list(recs: list) -> InlineKeyboardMarkup:
    rows = []
    for r in recs:
        icon = "⏸" if r.get("paused") else ("🟢" if r.get("active") else "⚪")
        rows.append([InlineKeyboardButton(text=f"{icon} {r.get('title', '')[:40]}", callback_data=f"rcv:{r['_id']}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tasks_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_recurring_card(rec: dict) -> InlineKeyboardMarkup:
    rid = str(rec["_id"])
    pause_btn = (
        InlineKeyboardButton(text="▶️ Відновити", callback_data=f"rcp:{rid}")
        if rec.get("paused")
        else InlineKeyboardButton(text="⏸ Пауза", callback_data=f"rcp:{rid}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"rce:{rid}"), pause_btn],
        [
            InlineKeyboardButton(text="📅 Змінити графік", callback_data=f"rcsch_open:r{rid}"),
            InlineKeyboardButton(text="❌ Скасувати наступне", callback_data=f"rcn:{rid}"),
        ],
        [InlineKeyboardButton(text="🗑 Видалити", callback_data=f"rcd:{rid}")],
        [InlineKeyboardButton(text="◀️ До списку", callback_data="rc_list")],
    ])


def ikb_recurring_edit(rid: str) -> InlineKeyboardMarkup:
    fields = [
        ("title", "📝 Назва"),
        ("description", "📄 Опис"),
        ("category", "🏷 Категорія"),
        ("priority", "🎨 Пріоритет"),
        ("time", "🕐 Час"),
        ("start", "▶️ Дата початку"),
        ("end", "🏁 Дата завершення"),
        ("reminder", "⏰ Нагадування"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rcf:{rid}:{key}")] for key, label in fields]
    rows.append([InlineKeyboardButton(text="📅 Частота і дні", callback_data=f"rcsch_open:r{rid}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"rcv:{rid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_rc_category(rid: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"rcfc:{rid}:{k}")]
        for k, c in CATEGORIES.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"rce:{rid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_rc_priority(rid: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"rcfl:{rid}:{k}")]
        for k, c in LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"rce:{rid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ikb_rc_confirm_delete(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Так, видалити", callback_data=f"rcdy:{rid}"),
            InlineKeyboardButton(text="◀️ Ні", callback_data=f"rcv:{rid}"),
        ],
    ])