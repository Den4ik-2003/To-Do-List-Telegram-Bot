from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto


async def resolve_channel(bot: Bot, ref: str):
    ref = ref.strip()
    if not ref:
        return None, "empty"
    if not ref.startswith("@") and not ref.lstrip("-").isdigit():
        ref = "@" + ref.lstrip("@")
    try:
        chat = await bot.get_chat(ref)
    except TelegramBadRequest:
        return None, "not_found"
    if chat.type != "channel":
        return chat, "not_channel"
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest:
        return chat, "not_member"
    if member.status != "administrator":
        return chat, "not_admin"
    if not getattr(member, "can_post_messages", False):
        return chat, "no_post_rights"
    return chat, "ok"


async def publish_post(bot: Bot, chat_id: int, rendered_text: str, photo_file_ids: list,
                        opening_sticker_file_id: str | None = None) -> list:
    message_ids = []
    if opening_sticker_file_id:
        m = await bot.send_sticker(chat_id, opening_sticker_file_id)
        message_ids.append(m.message_id)

    if not photo_file_ids:
        m = await bot.send_message(chat_id, rendered_text or ".")
        message_ids.append(m.message_id)
        return message_ids

    caption = rendered_text[:1024] if rendered_text else None
    if len(photo_file_ids) == 1:
        m = await bot.send_photo(chat_id, photo_file_ids[0], caption=caption)
        message_ids.append(m.message_id)
    else:
        media = [InputMediaPhoto(media=fid) for fid in photo_file_ids]
        if caption:
            media[0].caption = caption
        msgs = await bot.send_media_group(chat_id, media)
        message_ids.extend(m.message_id for m in msgs)

    if rendered_text and len(rendered_text) > 1024:
        m2 = await bot.send_message(chat_id, rendered_text)
        message_ids.append(m2.message_id)

    return message_ids