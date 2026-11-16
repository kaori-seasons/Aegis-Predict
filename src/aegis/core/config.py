from __future__ import annotations

import os
import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Settings(BaseSettings):
    """Runtime configuration. Every field can be overridden with an ``AEGIS_`` env var."""

    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    # Application role: must NOT own the tables and must NOT have BYPASSRLS, otherwise
    # row level security is silently skipped.
    database_url: str = "postgresql+psycopg://aegis_app@localhost:5432/aegis"
    # Owner / migration role. Used for migrations, cross-tenant job claiming and tenant bootstrap.
    admin_database_url: str = "postgresql+psycopg://localhost:5432/aegis"
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 60_000

    # Comma separated Fernet keys. The first key encrypts, all keys decrypt (rotation).
    secret_keys: str = ""

    auth_mode: Literal["google", "password", "hybrid"] = "google"
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = ""
    google_allowed_domains: list[str] = []
    integration_mode: Literal["mock", "live"] = "mock"
    require_reconciliation: bool = True

    session_ttl_minutes: int = 480
    session_cookie_name: str = "aegis_session"
    csrf_cookie_name: str = "aegis_csrf"
    cookie_secure: bool = True
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    file_inbox_root: Path = Path("./var/inbox")
    log_level: str = "INFO"
    log_json: bool = True

    worker_id: str = Field(default_factory=_default_worker_id)
    lease_seconds: int = 300
    worker_poll_seconds: float = 2.0
    sync_batch_size: int = 500
    normalize_batch_size: int = 1000
    circuit_failure_threshold: int = 5
    circuit_base_pause_seconds: int = 60
    circuit_max_pause_seconds: int = 3600
    sync_run_max_attempts: int = 5
    writeback_max_attempts: int = 8
    writeback_base_backoff_seconds: int = 30
    plan_run_max_attempts: int = 3
    writeback_enabled: bool = False
    plan_execution_enabled: bool = False

    # Connector network policy (SSRF guard for tenant-configured URLs).
    connector_allow_insecure_http: bool = False
    connector_allow_private_network: bool = False

    # Optional subscriber that receives outbox events (HMAC signed).
    event_webhook_url: str | None = None
    event_webhook_secret: str | None = None

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.env == "prod":
            if not self.secret_keys.strip():
                raise ValueError("AEGIS_SECRET_KEYS is required in prod")
            if not self.cookie_secure:
                raise ValueError("AEGIS_COOKIE_SECURE must be true in prod")
            if self.connector_allow_insecure_http or self.connector_allow_private_network:
                raise ValueError("insecure connector network policy is not allowed in prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
