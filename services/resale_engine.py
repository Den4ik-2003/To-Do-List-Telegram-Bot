"""
AI Resale Hunter — ядро аналізу оголошень OLX для перепродажу.

Тут навмисно винесено ВСЮ логіку побудови промптів, форматування відповіді
та калькуляції — окремо від aiogram-хендлерів (handlers/olx.py), щоб:
  1) хендлер відповідав тільки за UX/keyboard/FSM;
  2) цю логіку було легко покрити тестами;
  3) додавання нового майданчика (FB Marketplace, Prom, Allegro) не
     вимагало переписувати аналіз — досить дати сюди ту саму структуру
     листингу (title/price/description/photos/params).
"""

import logging

from services import ai_service

logger = logging.getLogger("tasks_bot")

DEFAULT_MARGIN_PERCENT = 20  # типова маржа перекупника, якщо користувач не задав свою


# =========================================================
# 1. Побудова промпту для повного resale-аналізу
# =========================================================

def build_analysis_prompt(listing: dict, min_margin_percent: float | None = None) -> str:
    """
    listing: {
      "source": "olx.ua" | "olx.pl" | ...,   # для майбутньої мультиплатформності
      "id"/"url": унікальний ідентифікатор — ОБОВ'ЯЗКОВО в промпті,
      "title", "price", "currency", "description", "location_text",
      "views", "photos" (list[url]), "photos_count", "params" (list[str]),
    }
    """
    margin = min_margin_percent if min_margin_percent is not None else DEFAULT_MARGIN_PERCENT
    params_text = "\n".join(f"- {p}" for p in (listing.get("params") or [])) or "(не вказані окремо)"
    description = (listing.get("description") or "(опис відсутній)")[:1500]
    photos = listing.get("photos") or []
    photos_total = listing.get("photos_count") if listing.get("photos_count") is not None else len(photos)

    # Фікс "однакових відповідей": явно вбиваємо в промпт унікальний ID/URL
    # оголошення і вимагаємо базуватись ТІЛЬКИ на наданих нижче даних.
    return f"""Ти — досвідчений перекупник, який оцінює КОНКРЕТНЕ оголошення на OLX
перед покупкою для перепродажу. Аналізуй ЛИШЕ дані нижче, для цього
конкретного оголошення (ID/URL: {listing.get('url') or listing.get('id')}).
Не використовуй жодні висновки з інших оголошень.

Тобі надано {len(photos)} фото з {photos_total} наявних у оголошенні —
проаналізуй їх ВСІ РАЗОМ як єдиний товар: перевір, чи це справді один і
той самий предмет на всіх фото, визнач стан, дефекти, комплектацію,
текст на коробках/етикетках якщо видно, різні ракурси. Якщо фото немає
або воно одне — прямо познач це в полі "photos_analyzed" і не вигадуй
деталей, яких не видно.

КРИТИЧНО: якщо не впевнений у бренді/моделі/ціні — НЕ вигадуй. Вкажи
нижчу впевненість (confidence) і перелічи, чого не вистачає (missing_data).

Відповідай ЛИШЕ у форматі JSON, без пояснень поза ним, за такою схемою:
{{
  "confidence_percent": число 0-100,
  "photos_analyzed": число,
  "item_name": "назва товару",
  "item_brand": "бренд або null",
  "item_model": "модель або null",
  "item_condition": "стан товару своїми словами",
  "defects": ["список видимих дефектів, або порожній масив"],
  "authenticity_concern": true/false,
  "authenticity_note": "коротко чому є/немає підозри на копію, або null",
  "new_price_uah_equivalent": число або null,
  "resale_price_min": число,
  "resale_price_max": число,
  "liquidity": "висока" | "середня" | "низька",
  "sale_speed": "швидко" | "середньо" | "довго",
  "resale_score": число 0-100,
  "verdict": "купувати" | "розглянути" | "не варто",
  "expected_profit": число,
  "roi_percent": число,
  "hidden_deal": true/false,
  "negotiation": {{
    "seller_price": {listing.get('price')},
    "soft_offer": число,
    "optimal_offer": число,
    "max_offer": число,
    "arguments": ["конкретні аргументи для торгу на основі ЦЬОГО оголошення"],
    "deal_success_probability_percent": число,
    "target_price_probability_percent": число
  }},
  "risks": {{
    "level": "низький" | "середній" | "високий",
    "notes": ["конкретні ризики цього оголошення"]
  }},
  "missing_data": ["чого не вистачає для точнішого аналізу, або порожній масив"]
}}

Розрахунки:
- expected_profit = (середина діапазону resale_price) - ціна продавця - орієнтовні
  витрати на доставку/чистку/ремонт, якщо вони очевидні з опису/фото.
- roi_percent = expected_profit / ціна_покупки * 100.
- Користувач хоче мінімальну маржу {margin}%. Якщо угода не дає такої маржі
  навіть при optimal_offer — це видно з verdict і resale_score, а не приховується.
- resale_score враховує ОДНОЧАСНО: маржу, ROI, абсолютний прибуток, ліквідність,
  ризики і складність перепродажу — не лише відсоток прибутку.
- negotiation.arguments — конкретні причини (довго висить, дефекти, немає
  коробки/комплектації, ціна вища за ринок тощо), а НЕ загальні фрази.

Дані оголошення:
Джерело: {listing.get('source', 'olx.ua')}
Назва: {listing.get('title') or '(без назви)'}
Ціна продавця: {listing.get('price')} {listing.get('currency', 'UAH')}
Локація: {listing.get('location_text') or 'не вказано'}
Переглядів: {listing.get('views') if listing.get('views') is not None else 'невідомо'}
Фото в оголошенні: {photos_total} (передано на аналіз: {len(photos)})
Характеристики:
{params_text}

Опис від продавця:
{description}"""


async def _call_ai_json(prompt: str, images: list[str] | None, temperature: float) -> dict | None:
    if not images:
        logger.info("resale_engine: аналіз без фото (0 доступних)")
    return await ai_service.generate_json(prompt, temperature=temperature, images=images)


async def analyze_listing(listing: dict, min_margin_percent: float | None = None) -> dict | None:
    """Головна точка входу: повний AI resale-аналіз одного оголошення."""
    prompt = build_analysis_prompt(listing, min_margin_percent)
    photos = listing.get("photos") or []
    result = await _call_ai_json(prompt, images=photos or None, temperature=0.4)
    if not result:
        return None

    if len(photos) <= 1:
        result["photos_analyzed"] = len(photos)
        result["limited_photo_warning"] = True
    else:
        result.setdefault("photos_analyzed", len(photos))
        result["limited_photo_warning"] = False

    result["photos_total"] = listing.get("photos_count") or len(photos)
    return result


# =========================================================
# 2. Форматування результату для Telegram
# =========================================================

_VERDICT_EMOJI = {"купувати": "🟢", "розглянути": "🟡", "не варто": "🔴"}
_RISK_EMOJI = {"низький": "🟢", "середній": "🟡", "високий": "🔴"}
_SPEED_EMOJI = {"швидко": "🟢", "середньо": "🟡", "довго": "🔴"}


def format_analysis(listing: dict, a: dict, cached: bool = False) -> str:
    currency = listing.get("currency", "UAH")
    score = a.get("resale_score")
    verdict = a.get("verdict", "невідомо")
    lines = []

    hidden_tag = " 💎 HIDDEN DEAL" if a.get("hidden_deal") else ""
    if score is not None:
        lines.append(f"🔥 *RESALE SCORE: {score}/100*{hidden_tag}")
    lines.append("")
    lines.append(f"📦 *Товар:* {a.get('item_name') or listing.get('title') or '—'}")
    if a.get("item_brand") or a.get("item_model"):
        lines.append(f"🏷 {a.get('item_brand') or ''} {a.get('item_model') or ''}".strip())
    lines.append(f"💰 *Ціна:* {listing.get('price')} {currency}")
    if a.get("resale_price_min") is not None and a.get("resale_price_max") is not None:
        lines.append(f"📊 *Ринок:* ~{a['resale_price_min']:.0f}–{a['resale_price_max']:.0f} {currency}")
    lines.append("")

    verdict_emoji = _VERDICT_EMOJI.get(verdict, "⚪️")
    lines.append(f"{verdict_emoji} *Рекомендація:* {verdict.upper()}")
    lines.append("")
    if a.get("expected_profit") is not None:
        lines.append(f"💵 *Очікуваний прибуток:* ~{a['expected_profit']:.0f} {currency}")
    if a.get("roi_percent") is not None:
        lines.append(f"📈 *ROI:* ~{a['roi_percent']:.0f}%")
    if a.get("liquidity"):
        lines.append(f"⚡ *Ліквідність:* {a['liquidity']}")
    if a.get("sale_speed"):
        lines.append(f"{_SPEED_EMOJI.get(a['sale_speed'], '⚪️')} *Швидкість продажу:* {a['sale_speed']}")
    lines.append("")

    neg = a.get("negotiation") or {}
    if neg:
        lines.append(f"🎯 *Оптимальна ціна покупки:* {neg.get('optimal_offer', '?')} {currency}")
        lines.append(f"🔴 *Максимум:* {neg.get('max_offer', '?')} {currency}")
        lines.append("")
        lines.append("💬 *Торг:*")
        lines.append(f"Старт: {neg.get('soft_offer', '?')} {currency}")
        lines.append(f"Оптимально: {neg.get('optimal_offer', '?')} {currency}")
        lines.append(f"Максимум: {neg.get('max_offer', '?')} {currency}")
        if neg.get("deal_success_probability_percent") is not None:
            lines.append(f"Ймовірність домовитись: {neg['deal_success_probability_percent']:.0f}%")
        if neg.get("target_price_probability_percent") is not None:
            lines.append(f"Ймовірність купити за бажаною ціною: {neg['target_price_probability_percent']:.0f}%")
        args = neg.get("arguments") or []
        if args:
            lines.append("")
            lines.append("🧠 *Аргументи для торгу:*")
            for arg in args[:5]:
                lines.append(f"• {arg}")

    risks = a.get("risks") or {}
    if risks:
        lines.append("")
        lines.append(f"⚠️ *Ризики:* {_RISK_EMOJI.get(risks.get('level'), '⚪️')} {risks.get('level', '—')}")
        for note in (risks.get("notes") or [])[:4]:
            lines.append(f"• {note}")

    defects = a.get("defects") or []
    if defects:
        lines.append("")
        lines.append("🔧 *Дефекти:* " + "; ".join(defects[:5]))

    if a.get("authenticity_concern"):
        lines.append(f"\n🚩 *Підозра на копію:* {a.get('authenticity_note') or 'так'}")

    missing = a.get("missing_data") or []
    if missing:
        lines.append("")
        lines.append("⚠️ *Недостатньо даних для точного аналізу:* " + "; ".join(missing[:4]))

    lines.append("")
    photos_analyzed = a.get("photos_analyzed", 0)
    photos_total = a.get("photos_total", photos_analyzed)
    if a.get("limited_photo_warning"):
        lines.append(f"⚠️ *Аналіз обмежений:* доступне лише {photos_total or 1} фото")
    else:
        lines.append(f"📸 *Аналіз фото:* {photos_analyzed}/{photos_total}")
    if a.get("confidence_percent") is not None:
        lines.append(f"🎯 *Впевненість AI:* {a['confidence_percent']:.0f}%")

    if cached:
        lines.append("\n_Оцінка з кешу (оголошення не змінювалось)_")

    return "\n".join(lines)


# =========================================================
# 3. Генерація повідомлень продавцю (3 стратегії торгу)
# =========================================================

_NEGOTIATION_STYLES = {
    "soft": "М'який торг — невелика знижка, максимальний шанс домовитись.",
    "optimal": "Оптимальний торг — баланс між знижкою і шансом на згоду продавця.",
    "aggressive": "Агресивний торг — максимально вигідна ціна, нижча ймовірність згоди.",
}


def build_negotiation_prompt(listing: dict, analysis: dict) -> str:
    neg = analysis.get("negotiation") or {}
    args = "; ".join((neg.get("arguments") or [])[:5]) or "загальні: товар цікавий, готовий забрати швидко"
    return f"""Напиши 3 варіанти короткого повідомлення продавцю на OLX для торгу
за товар "{listing.get('title')}" (ціна продавця: {listing.get('price')} {listing.get('currency', 'UAH')}).

Пропозиції для кожного стилю:
- м'який торг: {neg.get('soft_offer', '?')} {listing.get('currency', 'UAH')}
- оптимальний торг: {neg.get('optimal_offer', '?')} {listing.get('currency', 'UAH')}
- агресивний торг: {neg.get('max_offer', '?')} {listing.get('currency', 'UAH')}

Доступні аргументи для торгу (використай природно, не всі одразу):
{args}

Вимоги: українською, ввічливо, природно, як реальний покупець (не шаблонно),
1-3 речення на повідомлення, без емодзі-спаму. Кожне повідомлення повинно
відрізнятись тоном відповідно до стилю торгу.

Відповідай ЛИШЕ JSON:
{{"soft": "текст", "optimal": "текст", "aggressive": "текст"}}"""


async def generate_negotiation_messages(listing: dict, analysis: dict) -> dict | None:
    prompt = build_negotiation_prompt(listing, analysis)
    result = await ai_service.generate_json(prompt, temperature=0.7)
    if not result or not all(k in result for k in ("soft", "optimal", "aggressive")):
        return None
    return result


def format_negotiation_messages(messages: dict) -> str:
    lines = ["💬 *Готові повідомлення для торгу:*", ""]
    for key, label in (("soft", "🟢 М'який торг"), ("optimal", "🟡 Оптимальний торг"), ("aggressive", "🔴 Агресивний торг")):
        text = messages.get(key)
        if not text:
            continue
        lines.append(f"*{label}:*")
        lines.append(f"_{text}_")
        lines.append("")
    return "\n".join(lines).strip()


# =========================================================
# 4. Калькулятор перепродажу
# =========================================================

def calculate_resale(
    buy_price: float,
    delivery: float = 0.0,
    repair: float = 0.0,
    commission_percent: float = 0.0,
    sell_price: float | None = None,
    target_margin_percent: float | None = None,
) -> dict:
    """
    Незалежний від AI детермінований розрахунок — навмисно НЕ через LLM,
    щоб цифри були точні й відтворювані (AI лишається для оцінки ринкової
    ціни, а не для арифметики).
    """
    cost_base = buy_price + delivery + repair
    commission = (sell_price or 0) * commission_percent / 100 if sell_price else 0
    total_cost = cost_base + commission

    result = {
        "cost_base": round(cost_base, 2),
        "commission": round(commission, 2),
        "total_cost": round(total_cost, 2),
    }

    if sell_price is not None:
        profit = sell_price - total_cost
        roi = (profit / total_cost * 100) if total_cost else 0
        margin = (profit / sell_price * 100) if sell_price else 0
        result.update({
            "profit": round(profit, 2),
            "roi_percent": round(roi, 2),
            "margin_percent": round(margin, 2),
        })
        result["breakeven_price"] = round(total_cost, 2)

    if target_margin_percent is not None:
        if sell_price:
            extra_costs = delivery + repair
            commission_share = commission_percent / 100
            max_buy = sell_price * (1 - commission_share - target_margin_percent / 100) - extra_costs
            result["max_buy_price"] = round(max_buy, 2)

    return result


def format_calculation(calc: dict, currency: str = "UAH") -> str:
    lines = ["🧮 *Розрахунок перепродажу*", ""]
    lines.append(f"Собівартість (без комісії): {calc['cost_base']:.0f} {currency}")
    if calc.get("commission"):
        lines.append(f"Комісія: {calc['commission']:.0f} {currency}")
    lines.append(f"Повна собівартість: {calc['total_cost']:.0f} {currency}")
    if "profit" in calc:
        lines.append("")
        lines.append(f"💵 Прибуток: {calc['profit']:.0f} {currency}")
        lines.append(f"📈 ROI: {calc['roi_percent']:.1f}%")
        lines.append(f"📊 Маржа: {calc['margin_percent']:.1f}%")
        lines.append(f"⚖️ Точка беззбитковості: {calc['breakeven_price']:.0f} {currency}")
    if "max_buy_price" in calc:
        lines.append(f"\n🎯 Максимальна ціна покупки для заданої маржі: {calc['max_buy_price']:.0f} {currency}")
    return "\n".join(lines)


# =========================================================
# 5. ДОДАНО: AI-генерація оголошення на перепродаж (п.16 ТЗ)
# =========================================================

def build_listing_generation_prompt(listing: dict, analysis: dict, target_sell_price: float | None = None) -> str:
    """
    Використовує дані попереднього resale-аналізу (без вигадування нових
    характеристик) — щоб оголошення на перепродаж чесно відображало
    реальний стан і дефекти купленого товару.
    """
    condition = analysis.get("item_condition") or "не вказано"
    defects = "; ".join(analysis.get("defects") or []) or "не виявлено"
    brand = analysis.get("item_brand") or ""
    model = analysis.get("item_model") or ""
    resale_min = analysis.get("resale_price_min")
    resale_max = analysis.get("resale_price_max")
    currency = listing.get("currency", "UAH")

    price_hint = ""
    if target_sell_price:
        price_hint = f"Бажана ціна продажу від користувача: {target_sell_price} {currency}."
    elif resale_min is not None and resale_max is not None:
        price_hint = f"Орієнтовний ринковий діапазон з попереднього аналізу: {resale_min}-{resale_max} {currency}."

    return f"""Створи готове оголошення для перепродажу товару на OLX, українською мовою.

Дані про товар (з попереднього AI-аналізу перед покупкою — НЕ вигадуй нічого поверх цього):
Назва: {analysis.get('item_name') or listing.get('title')}
Бренд/модель: {brand} {model}
Стан: {condition}
Дефекти: {defects}
{price_hint}

Створи привабливе, чесне оголошення. Дефекти (якщо є) НЕ приховуй — вкажи
їх коротко і нейтрально, це ринок вживаних товарів, довіра важливіша за
замовчування.

Відповідай ЛИШЕ JSON:
{{
  "title": "приваблива назва оголошення, до 60 символів",
  "description": "опис 3-6 речень: стан, переваги, чесно про дефекти якщо є, чому варто купити",
  "characteristics": ["ключова характеристика 1", "..."],
  "advantages": ["перевага для покупця 1", "..."],
  "start_price": число (стартова ціна, трохи вище ринку — простір для торгу),
  "fast_sale_price": число (ціна для швидкого продажу — по ринку),
  "min_price": число (мінімальна прийнятна ціна)
}}"""


async def generate_resale_listing(listing: dict, analysis: dict, target_sell_price: float | None = None) -> dict | None:
    prompt = build_listing_generation_prompt(listing, analysis, target_sell_price)
    result = await ai_service.generate_json(prompt, temperature=0.6)
    required = ("title", "description", "start_price", "fast_sale_price", "min_price")
    if not result or not all(k in result for k in required):
        return None
    return result


def _fmt_price(value, currency: str) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.0f} {currency}"
    return f"{value} {currency}"


def format_resale_listing(data: dict, currency: str = "UAH") -> str:
    lines = ["📸 *Готове оголошення для перепродажу*", "", f"*{data.get('title', '')}*", "", data.get("description", "")]

    chars = data.get("characteristics") or []
    if chars:
        lines.append("")
        lines.append("📋 *Характеристики:*")
        for c in chars[:8]:
            lines.append(f"• {c}")

    advs = data.get("advantages") or []
    if advs:
        lines.append("")
        lines.append("✅ *Переваги:*")
        for adv in advs[:5]:
            lines.append(f"• {adv}")

    lines.append("")
    lines.append(f"🏷️ Стартова ціна: {_fmt_price(data.get('start_price'), currency)}")
    lines.append(f"⚡ Швидкий продаж: {_fmt_price(data.get('fast_sale_price'), currency)}")
    lines.append(f"🛑 Мінімум: {_fmt_price(data.get('min_price'), currency)}")
    return "\n".join(lines)


# =========================================================
# 6. ДОДАНО: TOP DEALS — порівняння вже проаналізованих оголошень (п.14 ТЗ)
# =========================================================

_MEDALS = ["🥇", "🥈", "🥉"]


def rank_top_deals(trackers: list[dict], limit: int = 3) -> list[dict]:
    """
    trackers: документи з database.olx (тип listing, з заповненим
    resale_analysis). Рейтинг детермінований (за resale_score, який уже
    враховує прибуток/ROI/ліквідність/ризик) — без додаткового AI-запиту.
    """
    scored = []
    for t in trackers:
        a = t.get("resale_analysis")
        if not a or a.get("resale_score") is None:
            continue
        scored.append((a.get("resale_score", 0), t, a))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"tracker": t, "analysis": a, "score": score} for score, t, a in scored[:limit]]


def format_top_deals(ranked: list[dict], currency: str = "UAH") -> str:
    if not ranked:
        return "📭 Ще немає проаналізованих оголошень для порівняння. Спочатку зроби AI-аналіз кількох товарів."

    lines = ["🏆 *TOP DEALS*", ""]
    for i, item in enumerate(ranked):
        medal = _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."
        t, a = item["tracker"], item["analysis"]
        title = a.get("item_name") or t.get("title") or "—"
        profit = a.get("expected_profit")
        profit_text = f"~{profit:.0f} {currency}" if profit is not None else "невідомо"
        lines.append(f"{medal} *{title}*")
        lines.append(f"Resale Score: {item['score']}/100")
        lines.append(f"Прибуток: {profit_text}")
        if a.get("liquidity"):
            lines.append(f"Ліквідність: {a['liquidity']}")
        lines.append("")

    best = ranked[0]
    best_title = best["analysis"].get("item_name") or best["tracker"].get("title") or "перший варіант"
    lines.append(
        f"💡 Найкращий варіант — *{best_title}*: найвищий Resale Score серед проаналізованих, "
        f"що враховує одночасно прибуток, ROI, ліквідність і ризик."
    )
    return "\n".join(lines).strip()


# =========================================================
# 7. ДОДАНО: рекомендації в межах бюджету (п.15 ТЗ)
# =========================================================

_RISK_PENALTY = {"низький": 0, "середній": 10, "високий": 25}


def recommend_purchases_within_budget(trackers: list[dict], budget: float) -> list[dict]:
    """
    Детермінований (без AI) відбір: серед відстежуваних оголошень
    (status=watching) з наявним resale_analysis обирає ті, що влазять у
    бюджет, сортуючи за resale_score, скоригованим на рівень ризику — а
    НЕ просто за найбільшим ROI, як прямо вимагає п.15 ТЗ.
    """
    candidates = []
    for t in trackers:
        if t.get("status") != "watching":
            continue
        a = t.get("resale_analysis")
        price = t.get("last_price")
        if not a or price is None or price > budget:
            continue
        risk_level = (a.get("risks") or {}).get("level", "середній")
        adjusted = a.get("resale_score", 0) - _RISK_PENALTY.get(risk_level, 10)
        candidates.append((adjusted, t, a))
    candidates.sort(key=lambda x: x[0], reverse=True)

    picks = []
    remaining = budget
    for adjusted, t, a in candidates:
        price = t.get("last_price", 0)
        if price <= remaining:
            picks.append({"tracker": t, "analysis": a, "adjusted_score": adjusted})
            remaining -= price
    return picks


def format_budget_recommendation(picks: list[dict], budget: float, currency: str = "UAH") -> str:
    if not picks:
        return (
            f"📭 У межах бюджету {budget:.0f} {currency} серед відстежуваних оголошень "
            f"немає підходящих варіантів (або вони ще не проаналізовані)."
        )

    lines = [f"💰 *Рекомендації в межах бюджету {budget:.0f} {currency}*", ""]
    total_cost = 0.0
    total_profit = 0.0
    for p in picks:
        t, a = p["tracker"], p["analysis"]
        price = t.get("last_price", 0)
        profit = a.get("expected_profit") or 0
        total_cost += price
        total_profit += profit
        title = a.get("item_name") or t.get("title") or "—"
        lines.append(f"✅ *{title}* — {price:.0f} {currency}")
        lines.append(f"   Resale Score: {a.get('resale_score', '?')}/100, прибуток ~{profit:.0f} {currency}")

    lines.append("")
    lines.append(f"📦 Разом купівля: ~{total_cost:.0f} {currency}")
    lines.append(f"💵 Очікуваний сумарний прибуток: ~{total_profit:.0f} {currency}")
    return "\n".join(lines)