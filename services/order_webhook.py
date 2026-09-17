import logging

from aiohttp import web

from database import orders as orders_db
from database import websites as websites_db
from services import order_notify_service

logger = logging.getLogger("tasks_bot")

MAX_FIELD_LEN = 300
_FIXED_FIELDS = {"name", "phone", "product", "comment"}


def _clean(value) -> str:
    return str(value or "").strip()[:MAX_FIELD_LEN]


async def handle_order(request: web.Request) -> web.Response:
    site_id = request.match_info.get("site_id", "")
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

    name = _clean(payload.get("name"))
    phone = _clean(payload.get("phone"))
    if not name or not phone:
        return web.json_response({"ok": False, "error": "missing_fields"}, status=400)

    order = {
        "name": name,
        "phone": phone,
        "product": _clean(payload.get("product")),
        "comment": _clean(payload.get("comment")),
        "extra": {
            _clean(k): _clean(v)
            for k, v in payload.items()
            if k not in _FIXED_FIELDS and _clean(k) and _clean(v)
        },
    }

    config = await websites_db.get_notification_config(site_id)
    if not config:
        return web.json_response({"ok": False, "error": "site_not_found"}, status=404)

    order_id = await orders_db.create_order(site_id, config["uid"], order)

    bot = request.app.get("bot")
    if bot is not None:
        delivered, error = await order_notify_service.deliver_order_notification(config, order, bot)
    else:
        logger.error("У aiohttp app немає ключа 'bot' — неможливо доставити замовлення в Telegram")
        delivered, error = False, "bot_not_configured"

    if delivered:
        await orders_db.mark_delivered(order_id)
    else:
        await orders_db.mark_delivery_failed(order_id, error or "unknown_error")

    return web.json_response({"ok": True})


def setup_order_routes(app: web.Application, bot) -> None:
    app["bot"] = bot
    app.router.add_post("/order/{site_id}", handle_order)