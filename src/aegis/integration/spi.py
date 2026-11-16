from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..core.config import Settings
from .ratelimit import RateLimiter


@dataclass(frozen=True)
class SyncWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class RawItem:
    """One source record, landed verbatim. ``payload`` must use canonical template field names."""

    stream: str
    external_id: str
    payload: dict[str, Any]
    source_updated_at: datetime | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    """Cursor to persist *after* every preceding RawItem has been landed (same transaction)."""

    stream: str
    cursor: dict[str, Any]


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WritebackRequest:
    command_type: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SubmitResult:
    external_ref: str


@dataclass
class ConnCtx:
    """Everything a connector may use. Connectors never touch the database directly."""

    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    source_system: str
    config: dict[str, Any]
    secrets: dict[str, Any]
    settings: Settings
    rate_limiter: RateLimiter
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] | None = None
    save_secrets: Callable[[dict[str, Any]], None] | None = None


@runtime_checkable
class SourceConnector(Protocol):
    kind: ClassVar[str]
    version: ClassVar[str]

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a normalised config or raise ``ValueError``; called before persisting a connection."""

    def streams(self, config: dict[str, Any]) -> list[str]:
        """Canonical streams this connection can produce."""

    def check(self, ctx: ConnCtx) -> HealthResult: ...

    def read(
        self,
        ctx: ConnCtx,
        stream: str,
        cursor: dict[str, Any] | None,
        window: SyncWindow | None,
    ) -> Iterator[RawItem | Checkpoint]: ...

    def count(self, ctx: ConnCtx, stream: str, window: SyncWindow) -> int | None:
        """Source-side record count for reconciliation; ``None`` when unsupported."""


@runtime_checkable
class WritebackConnector(Protocol):
    def writeback_commands(self, config: dict[str, Any]) -> list[str]: ...

    def submit(self, ctx: ConnCtx, request: WritebackRequest) -> SubmitResult:
        """Create the external document. Must send ``idempotency_key`` as the external reference."""

    def lookup(self, ctx: ConnCtx, request: WritebackRequest) -> str | None:
        """Find a previously created document by idempotency key (used after ambiguous failures)."""
