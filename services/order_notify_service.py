import logging

import aiohttp
from aiogram import Bot

from services import github_crypto

logger = logging.getLogger("tasks_bot")

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _build_order_text(site_name: str, order: dict) -> str:
    return (
        "🛒 *Нове замовлення*\n"
        f"🌐 Сайт: {site_name}\n"
        f"👤 Ім'я: {order.get('name') or '—'}\n"
        f"📞 Телефон: {order.get('phone') or '—'}\n"
        f"📦 Товар: {order.get('product') or '—'}\n"
        f"💬 Коментар: {order.get('comment') or '—'}"
    )


async def _send_via_external_bot(token: str, chat_id: str, text: str) -> tuple[bool, str | None]:
    url = f"{TELEGRAM_API_BASE}{token}/sendMessage"
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and data.get("ok"):
                    return True, None
                return False, str(data.get("description") or f"HTTP {resp.status}")
    except Exception as e:
        logger.exception("Не вдалося надіслати замовлення через підключеного бота")
        return False, str(e)


async def deliver_order_notification(config: dict | None, order: dict, fallback_bot: Bot) -> tuple[bool, str | None]:
    if not config:
        return False, "site_not_found"

    site_name = config.get("site_name") or "сайт"
    text = _build_order_text(site_name, order)

    token_encrypted = config.get("bot_token_encrypted")
    chat_id = config.get("chat_id")
    if token_encrypted and chat_id:
        try:
            token = github_crypto.decrypt_token(token_encrypted)
        except Exception:
            logger.exception("Не вдалося розшифрувати токен бота для замовлень")
            token = None
        if token:
            ok, error = await _send_via_external_bot(token, chat_id, text)
            if ok:
                return True, None
            logger.warning("Доставка через підключеного бота не вдалась (%s), fallback на основний бот", error)
            try:
                await fallback_bot.send_message(
                    config["uid"],
                    f"⚠️ Не вдалося надіслати замовлення через підключеного бота (причина: {error}). Дані замовлення:\n\n{text}",
                )
            except Exception:
                logger.exception("Fallback-доставка замовлення теж не вдалась (uid=%s)", config.get("uid"))
            return False, error

    try:
        await fallback_bot.send_message(config["uid"], text)
        return True, None
    except Exception as e:
        logger.exception("Не вдалося доставити замовлення основним ботом (uid=%s)", config.get("uid"))
        return False, str(e)