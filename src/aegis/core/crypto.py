from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import get_settings
from .errors import ConfigurationError


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


@lru_cache(maxsize=4)
def _cipher(keys: str) -> MultiFernet:
    parts = [part.strip() for part in keys.split(",") if part.strip()]
    if not parts:
        raise ConfigurationError("AEGIS_SECRET_KEYS is not configured; connector secrets cannot be stored")
    return MultiFernet([Fernet(part.encode("ascii")) for part in parts])


def cipher() -> MultiFernet:
    return _cipher(get_settings().secret_keys)


def encrypt_json(value: dict[str, Any]) -> str:
    return cipher().encrypt(json.dumps(value, sort_keys=True).encode("utf-8")).decode("ascii")


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return json.loads(cipher().decrypt(token.encode("ascii")))
    except InvalidToken as exc:
        raise ConfigurationError("connector secret cannot be decrypted with the configured keys") from exc


def rotate(token: str) -> str:
    """Re-encrypt with the primary key (run after prepending a new key to AEGIS_SECRET_KEYS)."""

    return cipher().rotate(token.encode("ascii")).decode("ascii")


SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization", "app_key", "signature", "cookie")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if any(marker in str(key).lower() for marker in SENSITIVE_KEYS) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
