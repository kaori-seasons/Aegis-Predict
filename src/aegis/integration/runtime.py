"""Sync runtime: queue claiming, leases (fencing), checkpoints, circuit breaker (plan 5.4 / 5.8).

Concurrency model
-----------------
* ``sync_run`` rows are the queue. Workers claim with ``FOR UPDATE ... SKIP LOCKED`` using the owner
  connection (cross tenant) and take a *connection lease* in the same transaction, so at most one
  worker reads a connection at any time.
* All data work happens in tenant transactions (RLS enforced). Every flush re-validates and renews
  the lease (fencing): a worker that lost its lease can never move a cursor.
* Raw rows and the cursor commit in one transaction; a crash re-reads from the last checkpoint
  and the raw unique constraint absorbs the overlap.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.db import admin_session, tenant_session
from ..core.tenancy import reset_trace_id, set_trace_id
from ..core.util import utcnow
from ..models import IngestionBatch, IntegrationConnection, RawRecord, SyncRun, SyncStreamState
from .batches import fail_batch, finish_landing, start_batch
from .streams import parse_datetime
from . import outbox
from .connections import build_ctx
from .errors import AuthError, ContractError, LeaseLost, TransientError
from .normalize import NormalizeStats, normalize_batch
from .raw import land_batch
from .registry import get_connector
from .spi import Checkpoint, RawItem, SyncWindow
from .streams import ordered_streams

logger = logging.getLogger("aegis.sync")


@dataclass(frozen=True)
class ClaimedRun:
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    owner: str | None = None


# -- queue ---------------------------------------------------------------------------------


def claim_sync_run(settings: Settings | None = None, worker_id: str | None = None) -> ClaimedRun | None:
    settings = settings or get_settings()
    worker = f"{worker_id or settings.worker_id}:{uuid.uuid4().hex}"
    with admin_session() as session:
        now = utcnow()
        row = session.execute(
            select(SyncRun.id, SyncRun.tenant_id, SyncRun.connection_id)
            .join(IntegrationConnection, IntegrationConnection.id == SyncRun.connection_id)
            .where(
                SyncRun.status == "queued",
                IntegrationConnection.status == "active",
                or_(IntegrationConnection.paused_until.is_(None), IntegrationConnection.paused_until <= now),
                or_(IntegrationConnection.lease_expires_at.is_(None), IntegrationConnection.lease_expires_at < now),
            )
            .order_by(SyncRun.created_at)
            .limit(1)
            .with_for_update(of=[SyncRun, IntegrationConnection], skip_locked=True)
        ).first()
        if row is None:
            return None
        session.execute(
            update(IntegrationConnection)
            .where(IntegrationConnection.id == row.connection_id)
            .values(lease_owner=worker, lease_expires_at=now + timedelta(seconds=settings.lease_seconds))
        )
        session.execute(
            update(SyncRun)
            .where(SyncRun.id == row.id)
            .values(status="running", attempts=SyncRun.attempts + 1, started_at=now, error=None)
        )
        return ClaimedRun(row.id, row.tenant_id, row.connection_id, worker)


def sweep_expired_leases(settings: Settings | None = None) -> dict[str, int]:
    """Recover work whose worker died: running sync runs, solving plans, sending writebacks."""

    settings = settings or get_settings()
    with admin_session() as session:
        now = utcnow()
        stale = session.execute(
            select(SyncRun.id, SyncRun.attempts, SyncRun.connection_id)
            .join(IntegrationConnection, IntegrationConnection.id == SyncRun.connection_id)
            .where(
                SyncRun.status == "running",
                or_(IntegrationConnection.lease_expires_at.is_(None), IntegrationConnection.lease_expires_at < now),
            )
            .with_for_update(of=SyncRun, skip_locked=True)
        ).all()
        requeued = failed = 0
        for run_id, attempts, connection_id in stale:
            session.execute(update(IngestionBatch).where(IngestionBatch.sync_run_id == run_id,
                            IngestionBatch.status == "landing").values(status="failed", error="worker lease expired"))
            if attempts >= settings.sync_run_max_attempts:
                session.execute(
                    update(SyncRun)
                    .where(SyncRun.id == run_id)
                    .values(status="failed", finished_at=now, error="worker lease expired too many times")
                )
                failed += 1
            else:
                session.execute(update(SyncRun).where(SyncRun.id == run_id).values(status="queued"))
                requeued += 1
            session.execute(
                update(IntegrationConnection)
                .where(IntegrationConnection.id == connection_id, IntegrationConnection.lease_expires_at < now)
                .values(lease_owner=None, lease_expires_at=None)
            )
        from ..planning.lifecycle import sweep_plan_leases
        from .writeback import sweep_writeback_leases

        plans = sweep_plan_leases(session, settings, now)
        writebacks = sweep_writeback_leases(session, now)
    return {"sync_requeued": requeued, "sync_failed": failed, "plans_recovered": plans, "writebacks_recovered": writebacks}


def schedule_due_syncs() -> int:
    """Enqueue incremental runs for connections whose schedule is due (idempotent, multi-instance safe)."""

    created = 0
    with admin_session() as session:
        now = utcnow()
        due = session.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.status == "active",
                IntegrationConnection.schedule_interval_seconds.is_not(None),
                or_(IntegrationConnection.next_run_at.is_(None), IntegrationConnection.next_run_at <= now),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for connection in due:
            active = session.scalar(
                select(func.count())
                .select_from(SyncRun)
                .where(SyncRun.connection_id == connection.id, SyncRun.status.in_(("queued", "running")))
            )
            if not active:
                session.add(
                    SyncRun(
                        id=uuid.uuid4(),
                        tenant_id=connection.tenant_id,
                        connection_id=connection.id,
                        mode="incremental",
                        status="queued",
                        requested_by="scheduler",
                        stats={},
                    )
                )
                created += 1
            connection.next_run_at = now + timedelta(seconds=connection.schedule_interval_seconds or 3600)
    return created


def tenants_with_pending_raw(limit: int = 50) -> list[uuid.UUID]:
    with admin_session() as session:
        return list(
            session.scalars(
                select(RawRecord.tenant_id).where(RawRecord.normalize_status == "pending").distinct().limit(limit)
            )
        )


def normalize_tenant(
    tenant_id: uuid.UUID,
    *,
    connection_id: uuid.UUID | None = None,
    settings: Settings | None = None,
    max_batches: int | None = None,
    between_batches: Any = None,
) -> NormalizeStats:
    """Drain pending raw records for a tenant (each batch is its own transaction)."""

    settings = settings or get_settings()
    total = NormalizeStats()
    batches = 0
    while max_batches is None or batches < max_batches:
        with tenant_session(tenant_id) as session:
            stats = normalize_batch(session, tenant_id, connection_id=connection_id, limit=settings.normalize_batch_size)
        batches += 1
        total.add(stats)
        if stats.processed == 0:
            break
        if between_batches is not None:
            between_batches()
    return total


# -- execution -----------------------------------------------------------------------------


class _Fence:
    def __init__(self, connection_id: uuid.UUID, worker: str, lease_seconds: int) -> None:
        self.connection_id = connection_id
        self.worker = worker
        self.lease_seconds = lease_seconds

    def renew(self, session: Session) -> None:
        renewed = session.execute(
            update(IntegrationConnection)
            .where(
                IntegrationConnection.id == self.connection_id,
                IntegrationConnection.lease_owner == self.worker,
                IntegrationConnection.lease_expires_at > func.now(),
            )
            .values(lease_expires_at=func.now() + text(f"interval '{int(self.lease_seconds)} seconds'"))
            .returning(IntegrationConnection.id)
        ).first()
        if renewed is None:
            raise LeaseLost(f"lease on connection {self.connection_id} lost by {self.worker}")


def _save_cursor(session: Session, tenant_id: uuid.UUID, connection_id: uuid.UUID, stream: str, cursor: dict) -> None:
    statement = insert(SyncStreamState).values(
        connection_id=connection_id, stream=stream, tenant_id=tenant_id, cursor=cursor, status="ok"
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["connection_id", "stream"],
            set_={"cursor": statement.excluded.cursor, "updated_at": func.now()},
        )
    )


def _mark_stream(
    session: Session,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    stream: str,
    *,
    status: str,
    error: str | None = None,
    success: bool = False,
    last_record_at: Any = None,
) -> None:
    values: dict[str, Any] = {"status": status, "last_error": error, "updated_at": func.now()}
    if success:
        values["last_success_at"] = func.now()
    if last_record_at is not None:
        values["last_record_at"] = last_record_at
    statement = insert(SyncStreamState).values(
        connection_id=connection_id, stream=stream, tenant_id=tenant_id, status=status, last_error=error
    )
    session.execute(
        statement.on_conflict_do_update(index_elements=["connection_id", "stream"], set_=values)
    )


def _execute_sync_run(claim: ClaimedRun, settings: Settings | None = None, worker_id: str | None = None) -> str:
    """Run a claimed sync. Returns the final status (or ``lease_lost``)."""

    settings = settings or get_settings()
    worker = claim.owner or worker_id or settings.worker_id
    fence = _Fence(claim.connection_id, worker, settings.lease_seconds)
    tenant_id = claim.tenant_id

    with tenant_session(tenant_id) as session:
        run = session.get(SyncRun, claim.run_id)
        connection = session.get(IntegrationConnection, claim.connection_id)
        if run is None or connection is None:
            return "missing"
        trace_token = set_trace_id(run.trace_id or uuid.uuid4().hex)
        mode = run.mode
        window = SyncWindow(run.window_start, run.window_end) if run.window_start and run.window_end else None
        connector = get_connector(connection.kind)
        streams = ordered_streams(run.streams or connector.streams(connection.config))
        states = {
            state.stream: state
            for state in session.scalars(
                select(SyncStreamState).where(SyncStreamState.connection_id == connection.id)
            )
        }
        cursors = {name: (state.cursor if state else None) for name, state in ((s, states.get(s)) for s in streams)}
        halted = {name for name, state in states.items() if state.status == "halted"}
        ctx = build_ctx(connection, settings)
        fixed_now = utcnow()
        ctx.now = lambda: fixed_now

    def save_rotated_secrets(secrets: dict) -> None:
        from ..core.crypto import encrypt_json
        with tenant_session(tenant_id) as session:
            fence.renew(session)
            session.execute(update(IntegrationConnection).where(IntegrationConnection.id == claim.connection_id)
                            .values(secret_ciphertext=encrypt_json(secrets)))

    ctx.save_secrets = save_rotated_secrets

    counters = {"read": 0, "landed": 0, "duplicate": 0}
    stream_stats: dict[str, dict[str, Any]] = {}
    partial = False
    try:
        for stream in streams:
            if stream in halted and mode == "incremental":
                stream_stats[stream] = {"skipped": "halted"}
                partial = True
                continue
            stats = stream_stats.setdefault(stream, {"read": 0, "landed": 0, "duplicate": 0})
            buffer: list[RawItem] = []
            last_record_at = None
            checkpoint = None
            previous = (cursors.get(stream) or {}).get("updated_since") or ctx.config.get("initial_since")
            start_at = window.start if window else (parse_datetime(previous, "UTC") if previous else None)
            if window is None and start_at and connection.kind in ("amazon_sp", "tiktok_shop", "mock_amazon", "mock_tiktok"):
                start_at -= timedelta(minutes=ctx.config.get("lookback_minutes", 120))
            with tenant_session(tenant_id) as session:
                fence.renew(session)
                batch = start_batch(session, tenant_id, claim.connection_id, stream, sync_run_id=claim.run_id,
                                    window_start=start_at, window_end=window.end if window else None,
                                    scope={"order_window": stream == "order_lines" and connection.kind in ("amazon_sp", "tiktok_shop", "mock_amazon", "mock_tiktok"),
                                           "snapshot_at": fixed_now.isoformat(), "is_mock": connection.is_mock,
                                           "market": ctx.config.get("marketplace_id") or ctx.config.get("market"),
                                           "account": ctx.config.get("seller_id") or ctx.config.get("shop_id"),
                                           "location": ctx.config.get("location_code")})
                batch_id = batch.id

            def flush(cursor: dict | None) -> None:
                nonlocal buffer, checkpoint
                with tenant_session(tenant_id) as session:
                    fence.renew(session)
                    result = land_batch(
                        session, tenant_id=tenant_id, connection_id=claim.connection_id, sync_run_id=claim.run_id,
                        items=buffer, batch_id=batch_id
                    )
                    if cursor is not None:
                        checkpoint = cursor
                    session.execute(
                        update(SyncRun)
                        .where(SyncRun.id == claim.run_id)
                        .values(
                            records_read=SyncRun.records_read + len(buffer),
                            records_landed=SyncRun.records_landed + result.landed,
                            records_duplicate=SyncRun.records_duplicate + result.duplicates,
                        )
                    )
                stats["landed"] += result.landed
                stats["duplicate"] += result.duplicates
                counters["landed"] += result.landed
                counters["duplicate"] += result.duplicates
                buffer = []

            try:
                for item in connector.read(ctx, stream, cursors.get(stream) if mode == "incremental" else None, window):
                    if item.stream != stream:
                        raise ContractError("connector emitted a record or checkpoint for another stream")
                    if isinstance(item, Checkpoint):
                        flush(item.cursor)
                        continue
                    buffer.append(item)
                    stats["read"] += 1
                    counters["read"] += 1
                    if item.source_updated_at and (last_record_at is None or item.source_updated_at > last_record_at):
                        last_record_at = item.source_updated_at
                    if len(buffer) >= settings.sync_batch_size:
                        flush(None)
                if buffer:
                    flush(None)
            except ContractError as exc:
                logger.error("stream halted", extra={"stream": stream, "error": str(exc)})
                with tenant_session(tenant_id) as session:
                    fence.renew(session)
                    fail_batch(session, batch_id, str(exc))
                    _mark_stream(session, tenant_id, claim.connection_id, stream, status="halted", error=str(exc)[:4000])
                    outbox.emit(
                        session,
                        tenant_id=tenant_id,
                        topic="integration.stream.halted",
                        aggregate_type="integration_connection",
                        aggregate_id=claim.connection_id,
                        payload={"stream": stream, "error": str(exc)[:1000]},
                    )
                stats["halted"] = str(exc)[:500]
                partial = True
                continue
            with tenant_session(tenant_id) as session:
                fence.renew(session)
                finish_landing(session, batch_id)
                batch = session.get(IngestionBatch, batch_id)
                if checkpoint and checkpoint.get("updated_since"):
                    batch.window_end = parse_datetime(checkpoint["updated_since"], "UTC")
                # A partial stream never advances the online cursor. A retry can safely
                # relink already-landed raw records to its complete batch.
                if checkpoint is not None and mode == "incremental":
                    _save_cursor(session, tenant_id, claim.connection_id, stream, checkpoint)
                _mark_stream(
                    session,
                    tenant_id,
                    claim.connection_id,
                    stream,
                    status="ok",
                    success=mode == "incremental",
                    last_record_at=last_record_at,
                )

        def renew_between_batches() -> None:
            with tenant_session(tenant_id) as session:
                fence.renew(session)

        normalized = normalize_tenant(
            tenant_id, connection_id=claim.connection_id, settings=settings, between_batches=renew_between_batches
        )
        status = "partial" if partial else "succeeded"
        with tenant_session(tenant_id) as session:
            fence.renew(session)
            batches = list(session.scalars(select(IngestionBatch).where(IngestionBatch.sync_run_id == claim.run_id)))
            if any(batch.status != "complete" for batch in batches):
                status = "partial"
                for batch in batches:
                    if batch.status == "quarantined":
                        _mark_stream(session, tenant_id, claim.connection_id, batch.stream,
                                     status="error", error="batch contains quarantined or ignored records")
            session.execute(
                update(SyncRun)
                .where(SyncRun.id == claim.run_id)
                .values(
                    status=status,
                    finished_at=func.now(),
                    records_normalized=SyncRun.records_normalized + normalized.normalized,
                    records_quarantined=SyncRun.records_quarantined + normalized.quarantined,
                    stats={"streams": stream_stats, "normalize": normalized.by_stream, "replayed": normalized.replayed},
                )
            )
            values: dict[str, Any] = {"consecutive_failures": 0, "paused_until": None, "last_error": None}
            if mode == "incremental":
                values["last_success_at"] = func.now()
            session.execute(update(IntegrationConnection).where(IntegrationConnection.id == claim.connection_id).values(**values))
            outbox.emit(
                session,
                tenant_id=tenant_id,
                topic="integration.sync.finished",
                aggregate_type="sync_run",
                aggregate_id=claim.run_id,
                payload={"status": status, **counters, "normalized": normalized.normalized, "quarantined": normalized.quarantined},
            )
        _release(fence)
        return status
    except LeaseLost:
        logger.warning("lease lost; abandoning run", extra={"run_id": str(claim.run_id)})
        return "lease_lost"
    except AuthError as exc:
        _fail(claim, fence, settings, str(exc), auth=True)
        return "failed"
    except TransientError as exc:
        _fail(claim, fence, settings, str(exc))
        return "failed"
    except Exception as exc:  # noqa: BLE001 - recorded on the run, circuit breaker counts it
        logger.exception("sync run crashed", extra={"run_id": str(claim.run_id)})
        _fail(claim, fence, settings, f"{type(exc).__name__}: {exc}")
        return "failed"
    finally:
        reset_trace_id(trace_token)


def _fail(claim: ClaimedRun, fence: _Fence, settings: Settings, message: str, *, auth: bool = False) -> None:
    try:
        with tenant_session(claim.tenant_id) as session:
            fence.renew(session)
            connection = session.get(IntegrationConnection, claim.connection_id, with_for_update=True)
            session.execute(
                update(SyncRun)
                .where(SyncRun.id == claim.run_id)
                .values(status="failed", finished_at=func.now(), error=message[:4000])
            )
            connection.last_error = message[:4000]
            session.execute(update(IngestionBatch).where(IngestionBatch.sync_run_id == claim.run_id,
                            IngestionBatch.status == "landing").values(status="failed", error=message[:2000]))
            if auth:
                connection.status = "auth_failed"
                topic = "integration.connection.auth_failed"
            else:
                connection.consecutive_failures += 1
                topic = "integration.sync.failed"
                over = connection.consecutive_failures - settings.circuit_failure_threshold
                if over >= 0:
                    pause = min(settings.circuit_max_pause_seconds, settings.circuit_base_pause_seconds * (2**over))
                    connection.paused_until = utcnow() + timedelta(seconds=pause)
                    topic = "integration.connection.circuit_open"
            outbox.emit(
                session,
                tenant_id=claim.tenant_id,
                topic=topic,
                aggregate_type="integration_connection",
                aggregate_id=claim.connection_id,
                payload={"run_id": claim.run_id, "error": message[:1000], "failures": connection.consecutive_failures},
            )
    except LeaseLost:
        logger.warning("lease lost while recording failure", extra={"run_id": str(claim.run_id)})
        return
    _release(fence)


def _release(fence: _Fence) -> None:
    with admin_session() as session:
        session.execute(
            update(IntegrationConnection)
            .where(and_(IntegrationConnection.id == fence.connection_id, IntegrationConnection.lease_owner == fence.worker))
            .values(lease_owner=None, lease_expires_at=None)
        )


def run_sync_once(settings: Settings | None = None, worker_id: str | None = None) -> str | None:
    claim = claim_sync_run(settings, worker_id)
    if claim is None:
        return None
    return execute_sync_run(claim, settings, worker_id)


def execute_sync_run(claim: ClaimedRun, settings: Settings | None = None, worker_id: str | None = None) -> str:
    from .lease import heartbeat
    settings = settings or get_settings()
    fence = _Fence(claim.connection_id, claim.owner or worker_id or settings.worker_id, settings.lease_seconds)
    def renew():
        with tenant_session(claim.tenant_id) as session:
            fence.renew(session)
    try:
        with heartbeat(renew, settings.lease_seconds):
            return _execute_sync_run(claim, settings, worker_id)
    except LeaseLost:
        return "lease_lost"
    except Exception as exc:
        _fail(claim, fence, settings, f"{type(exc).__name__}: {exc}")
        return "failed"
