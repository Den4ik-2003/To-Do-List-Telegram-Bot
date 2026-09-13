import logging

from cryptography.fernet import Fernet, InvalidToken

from config.settings import GITHUB_TOKEN_ENCRYPTION_KEY

logger = logging.getLogger("tasks_bot")

_fernet = Fernet(GITHUB_TOKEN_ENCRYPTION_KEY.encode()) if GITHUB_TOKEN_ENCRYPTION_KEY else None


def is_available() -> bool:
    return _fernet is not None


def encrypt_token(token: str) -> str:
    if not _fernet:
        raise RuntimeError("GITHUB_TOKEN_ENCRYPTION_KEY не налаштовано")
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(enc_token: str) -> str | None:
    if not _fernet:
        return None
    try:
        return _fernet.decrypt(enc_token.encode()).decode()
    except InvalidToken:
        logger.error("GitHub token decryption failed — invalid token or wrong key")
        return None