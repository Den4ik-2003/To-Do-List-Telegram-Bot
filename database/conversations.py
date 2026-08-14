from database.mongo import ai_conversations_col, db_call

DEFAULT_MAX_MESSAGES = 20


async def get_conversation(uid: int) -> dict:
    doc = await db_call(ai_conversations_col.find_one({"uid": uid}), raise_on_fail=False)
    if not doc:
        return {"uid": uid, "messages": [], "summary": ""}
    doc.setdefault("messages", [])
    doc.setdefault("summary", "")
    return doc


async def append_message(uid: int, role: str, content: str, max_messages: int = DEFAULT_MAX_MESSAGES):
    convo = await get_conversation(uid)
    messages = convo.get("messages", [])
    messages.append({"role": role, "content": content})
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
    await db_call(
        ai_conversations_col.update_one(
            {"uid": uid},
            {"$set": {"uid": uid, "messages": messages}},
            upsert=True,
        )
    )


async def set_summary(uid: int, summary: str):
    await db_call(
        ai_conversations_col.update_one(
            {"uid": uid},
            {"$set": {"uid": uid, "summary": summary}},
            upsert=True,
        )
    )


async def clear_conversation(uid: int):
    await db_call(
        ai_conversations_col.update_one(
            {"uid": uid},
            {"$set": {"uid": uid, "messages": [], "summary": ""}},
            upsert=True,
        )
    )