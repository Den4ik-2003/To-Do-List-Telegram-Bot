import logging
from datetime import datetime

from database.mongo import netlify_credentials_col, db_call

logger = logging.getLogger("tasks_bot")


async def save_credential(uid: int, encrypted_token: str, netlify_email: str) -> None:
    await db_call(
        netlify_credentials_col.update_one(
            {"userId": uid},
            {"$set": {
                "userId": uid,
                "encryptedToken": encrypted_token,
                "netlifyEmail": netlify_email,
                "connectedAt": datetime.utcnow().isoformat(),
            }},
            upsert=True,
        ),
        raise_on_fail=False,
    )


async def get_credential(uid: int) -> dict | None:
    return await db_call(netlify_credentials_col.find_one({"userId": uid}), raise_on_fail=False)


async def delete_credential(uid: int) -> bool:
    result = await db_call(netlify_credentials_col.delete_one({"userId": uid}), raise_on_fail=False)
    return bool(result and result.deleted_count)