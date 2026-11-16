"""Connection management service (used by the API, CLI and workers)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.audit import record_audit
from ..core.config import Settings, get_settings
from ..core.crypto import decrypt_json, encrypt_json, redact
from ..core.errors import Conflict, InvalidInput, NotFound
from ..core.tenancy import Principal, current_trace_id
from ..core.util import content_hash, utcnow
from ..models import IntegrationConnection, SyncRun, SyncStreamState
from .ratelimit import RateLimiter
from .registry import get_connector
from .spi import ConnCtx
from .streams import ordered_streams

ACTIVE_RUN_STATUSES = ("queued", "running")
UNSET: Any = object()
_limiters: dict[uuid.UUID, tuple[str, RateLimiter]] = {}
_limiters_lock = threading.Lock()


def rate_limiter_for(connection: IntegrationConnection) -> RateLimiter:
    """Process-wide limiter per connection (the connection lease makes this single-writer)."""

    spec = (connection.config or {}).get("rate_limit") or {}
    rate = float(spec.get("rate_per_second") or 5.0)
    burst = float(spec.get("burst") or max(rate, 1.0))
    fingerprint = f"{rate}:{burst}"
    with _limiters_lock:
        cached = _limiters.get(connection.id)
        if cached is None or cached[0] != fingerprint:
            cached = (fingerprint, RateLimiter(rate, burst))
            _limiters[connection.id] = cached
        return cached[1]


def build_ctx(connection: IntegrationConnection, settings: Settings | None = None) -> ConnCtx:
    return ConnCtx(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        source_system=connection.source_system,
        config=dict(connection.config or {}),
        secrets=decrypt_json(connection.secret_ciphertext),
        settings=settings or get_settings(),
        rate_limiter=rate_limiter_for(connection),
    )


def get_connection(session: Session, connection_id: uuid.UUID, *, lock: bool = False) -> IntegrationConnection:
    query = select(IntegrationConnection).where(IntegrationConnection.id == connection_id)
    if lock:
        query = query.with_for_update()
    connection = session.scalar(query)
    if connection is None:
        raise NotFound("connection not found")
    return connection


def _validate(kind: str, config: dict[str, Any], settings: Settings) -> dict[str, Any]:
    connector = get_connector(kind)
    try:
        return connector.validate_config(config, settings)  # type: ignore[call-arg]
    except ValueError as exc:
        raise InvalidInput(f"invalid {kind} configuration: {exc}") from exc


def _validate_interval(value: int | None, label: str) -> None:
    if value is not None and value < 60:
        raise InvalidInput(f"{label} must be at least 60 seconds")


def connection_view(connection: IntegrationConnection, *, include_secret_keys: bool = True) -> dict[str, Any]:
    secret_keys: list[str] = []
    if include_secret_keys and connection.secret_ciphertext:
        try:
            secret_keys = sorted(decrypt_json(connection.secret_ciphertext))
        except Exception:  # noqa: BLE001 - a rotated-away key must not break listing
            secret_keys = ["<undecryptable>"]
    return {
        "id": str(connection.id),
        "name": connection.name,
        "kind": connection.kind,
        "is_mock": connection.is_mock,
        "source_system": connection.source_system,
        "status": connection.status,
        "config": redact(connection.config),
        "secret_keys": secret_keys,
        "schedule_interval_seconds": connection.schedule_interval_seconds,
        "next_run_at": connection.next_run_at,
        "reconcile_interval_seconds": connection.reconcile_interval_seconds,
        "last_reconciled_at": connection.last_reconciled_at,
        "consecutive_failures": connection.consecutive_failures,
        "paused_until": connection.paused_until,
        "last_error": connection.last_error,
        "last_success_at": connection.last_success_at,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def create_connection(
    session: Session,
    principal: Principal,
    *,
    name: str,
    kind: str,
    config: dict[str, Any],
    secrets: dict[str, Any] | None = None,
    source_system: str | None = None,
    schedule_interval_seconds: int | None = None,
    reconcile_interval_seconds: int | None = None,
    settings: Settings | None = None,
) -> IntegrationConnection:
    settings = settings or get_settings()
    name = name.strip()
    if not name:
        raise InvalidInput("name is required")
    _validate_interval(schedule_interval_seconds, "schedule_interval_seconds")
    _validate_interval(reconcile_interval_seconds, "reconcile_interval_seconds")
    normalized = _validate(kind, config, settings)
    from .markets import platform_source
    base_kind = {"mock_amazon": "amazon_sp", "mock_tiktok": "tiktok_shop"}.get(kind, kind)
    if kind in ("amazon_sp", "tiktok_shop") and settings.integration_mode == "mock":
        raise InvalidInput("live platform connections are disabled; use mock_amazon or mock_tiktok")
    identity_source = platform_source(base_kind, normalized)
    if identity_source and kind.startswith("mock_"):
        identity_source = "mock:"+identity_source
    if identity_source:
        if source_system is not None and source_system != identity_source:
            raise InvalidInput("platform source identity is derived from seller/shop and market, and cannot be overridden")
        source_system = identity_source
        if session.scalar(select(IntegrationConnection.id).where(IntegrationConnection.source_system == source_system)):
            raise Conflict("this platform account/market is already connected in this tenant")
    if session.scalar(select(IntegrationConnection.id).where(IntegrationConnection.name == name)):
        raise Conflict(f"connection '{name}' already exists")
    connection = IntegrationConnection(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        name=name,
        kind=kind,
        is_mock=kind.startswith("mock_") or (kind == "file_csv" and settings.integration_mode == "mock"),
        source_system=(source_system or normalized.get("source_system") or f"{kind}:{name}")[:80],
        config=normalized,
        secret_ciphertext=encrypt_json(secrets) if secrets else None,
        status="active",
        schedule_interval_seconds=schedule_interval_seconds,
        next_run_at=utcnow() if schedule_interval_seconds else None,
        reconcile_interval_seconds=reconcile_interval_seconds,
    )
    session.add(connection)
    session.flush()
    record_audit(
        session,
        principal,
        "integration.connection.create",
        "integration_connection",
        connection.id,
        after={**connection_view(connection, include_secret_keys=False), "secret_keys": sorted(secrets or {})},
    )
    return connection


def update_connection(
    session: Session,
    principal: Principal,
    connection: IntegrationConnection,
    *,
    config: dict[str, Any] | None = None,
    schedule_interval_seconds: int | None = UNSET,
    reconcile_interval_seconds: int | None = UNSET,
    status: str | None = None,
    settings: Settings | None = None,
) -> IntegrationConnection:
    before = connection_view(connection, include_secret_keys=False)
    if config is not None:
        normalized = _validate(connection.kind, config, settings or get_settings())
        from .markets import platform_source
        base_kind = {"mock_amazon": "amazon_sp", "mock_tiktok": "tiktok_shop"}.get(connection.kind, connection.kind)
        identity = platform_source(base_kind, normalized)
        if identity and ("mock:"+identity if connection.is_mock else identity) != connection.source_system:
            raise InvalidInput("account and market identity cannot be changed; create a new connection")
        if content_hash(normalized) != content_hash(connection.config):
            connection.config = normalized
    if schedule_interval_seconds is not UNSET:
        _validate_interval(schedule_interval_seconds, "schedule_interval_seconds")
        connection.schedule_interval_seconds = schedule_interval_seconds
        connection.next_run_at = utcnow() if schedule_interval_seconds else None
    if reconcile_interval_seconds is not UNSET:
        _validate_interval(reconcile_interval_seconds, "reconcile_interval_seconds")
        connection.reconcile_interval_seconds = reconcile_interval_seconds
    if status is not None:
        if status not in ("active", "paused", "disabled"):
            raise InvalidInput("status must be active, paused or disabled")
        connection.status = status
        if status == "active":
            connection.paused_until = None
            connection.consecutive_failures = 0
    session.flush()
    record_audit(
        session,
        principal,
        "integration.connection.update",
        "integration_connection",
        connection.id,
        before=before,
        after=connection_view(connection, include_secret_keys=False),
    )
    return connection


def set_secrets(session: Session, principal: Principal, connection: IntegrationConnection, secrets: dict[str, Any]) -> None:
    if not isinstance(secrets, dict) or not secrets:
        raise InvalidInput("secrets must be a non-empty object")
    connection.secret_ciphertext = encrypt_json(secrets)
    if connection.status == "auth_failed":
        connection.status = "active"
        connection.last_error = None
    record_audit(
        session,
        principal,
        "integration.connection.secrets.set",
        "integration_connection",
        connection.id,
        after={"secret_keys": sorted(secrets)},
    )


def resume_connection(session: Session, principal: Principal, connection: IntegrationConnection) -> None:
    before = {"status": connection.status, "paused_until": connection.paused_until}
    connection.status = "active"
    connection.paused_until = None
    connection.consecutive_failures = 0
    connection.last_error = None
    record_audit(
        session,
        principal,
        "integration.connection.resume",
        "integration_connection",
        connection.id,
        before=before,
        after={"status": "active", "paused_until": None},
    )


def request_sync(
    session: Session,
    principal: Principal,
    connection: IntegrationConnection,
    *,
    mode: str = "incremental",
    streams: list[str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    audit: bool = True,
) -> SyncRun:
    if mode not in ("incremental", "backfill"):
        raise InvalidInput("mode must be incremental or backfill")
    if connection.status in ("disabled", "auth_failed"):
        raise Conflict(f"connection is {connection.status}; fix it before syncing", code="connection_unavailable")
    available = set(get_connector(connection.kind).streams(connection.config))
    if streams:
        try:
            streams = ordered_streams(streams)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        missing = sorted(set(streams) - available)
        if missing:
            raise InvalidInput(f"streams not provided by this connection: {', '.join(missing)}")
    if mode == "backfill":
        if window_start is None or window_end is None or window_start >= window_end:
            raise InvalidInput("backfill requires window_start < window_end")
        if window_end - window_start > timedelta(days=400):
            raise InvalidInput("backfill window must not exceed 400 days; split it")
    else:
        window_start = window_end = None
        existing = session.scalar(
            select(SyncRun).where(
                SyncRun.connection_id == connection.id,
                SyncRun.mode == "incremental",
                SyncRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if existing is not None:
            return existing
    run = SyncRun(
        id=uuid.uuid4(),
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        mode=mode,
        streams=streams or None,
        window_start=window_start,
        window_end=window_end,
        status="queued",
        requested_by=principal.label,
        trace_id=current_trace_id(),
        stats={},
    )
    session.add(run)
    session.flush()
    if audit:
        record_audit(
            session,
            principal,
            f"integration.sync.{mode}",
            "sync_run",
            run.id,
            after={"connection_id": connection.id, "streams": streams, "window_start": window_start, "window_end": window_end},
        )
    return run


def reset_stream(session: Session, principal: Principal, connection: IntegrationConnection, stream: str) -> None:
    """Clear cursor + halted state so the next incremental run re-reads the stream from scratch."""

    state = session.get(SyncStreamState, (connection.id, stream))
    if state is None:
        raise NotFound(f"stream '{stream}' has no state")
    before = {"cursor": state.cursor, "status": state.status}
    state.cursor = None
    state.status = "ok"
    state.last_error = None
    record_audit(
        session, principal, "integration.stream.reset", "sync_stream_state", f"{connection.id}:{stream}", before=before, after={"cursor": None, "status": "ok"}
    )


def cancel_queued_runs(session: Session, connection_id: uuid.UUID) -> int:
    result = session.execute(
        update(SyncRun)
        .where(SyncRun.connection_id == connection_id, SyncRun.status == "queued")
        .values(status="cancelled", finished_at=utcnow(), error="cancelled")
    )
    return result.rowcount or 0
