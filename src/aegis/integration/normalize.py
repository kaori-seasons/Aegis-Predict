"""Raw -> canonical normalisation, quarantine and replay (plan 5.1 / 5.6).

Each call processes one batch of pending raw records inside the caller's tenant transaction. Raw
rows are locked with ``FOR UPDATE SKIP LOCKED`` so several workers can normalise concurrently.
Records are applied in canonical stream order (master data first) and within a stream in landing
order; ordering across batches is not required because canonical upserts are version guarded and
unmapped records are replayed automatically when master data changes.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session

from ..core.util import utcnow
from ..models import BatchRecord, IngestionBatch, IntegrationConnection, QuarantineRecord, RawRecord
from . import outbox
from .canonical import MAPPER_VERSION, CanonicalRow, UpsertResult, build_row, upsert_rows
from .mapping import MappingContext, MappingIssue
from .quality import QualityChecker
from .streams import MASTER_STREAMS, STREAMS, FieldError, ordered_streams, parse_record

logger = logging.getLogger("aegis.normalize")

UNMAPPED_REASONS = ("unmapped_sku", "unmapped_location")


@dataclass
class NormalizeStats:
    processed: int = 0
    normalized: int = 0
    quarantined: int = 0
    changed: int = 0
    replayed: int = 0
    by_stream: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, other: "NormalizeStats") -> None:
        self.processed += other.processed
        self.normalized += other.normalized
        self.quarantined += other.quarantined
        self.changed += other.changed
        self.replayed += other.replayed
        for stream, counts in other.by_stream.items():
            target = self.by_stream.setdefault(stream, {})
            for key, value in counts.items():
                target[key] = target.get(key, 0) + value


def _quarantine(session: Session, raw: RawRecord, reason_code: str, detail: str) -> None:
    session.add(
        QuarantineRecord(
            tenant_id=raw.tenant_id,
            raw_record_id=raw.id,
            connection_id=raw.connection_id,
            stream=raw.stream,
            reason_code=reason_code,
            detail=detail[:4000],
        )
    )


def normalize_batch(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    connection_id: uuid.UUID | None = None,
    limit: int = 1000,
) -> NormalizeStats:
    """Normalise up to ``limit`` pending raw records. Returns zero ``processed`` when nothing is pending."""

    stats = NormalizeStats()
    session.flush()  # A caller may have sealed the last batch in this transaction.
    query = select(RawRecord).where(RawRecord.tenant_id == tenant_id, RawRecord.normalize_status == "pending")
    member = exists(select(BatchRecord.raw_record_id).where(BatchRecord.raw_record_id == RawRecord.id))
    ready = exists(select(BatchRecord.raw_record_id).join(IngestionBatch, IngestionBatch.id == BatchRecord.batch_id)
                   .where(BatchRecord.raw_record_id == RawRecord.id, IngestionBatch.status.in_(("landed", "complete", "quarantined"))))
    query = query.where(or_(~member, ready))
    if connection_id is not None:
        query = query.where(RawRecord.connection_id == connection_id)
    raws = session.scalars(query.order_by(RawRecord.id).limit(limit).with_for_update(skip_locked=True)).all()
    if not raws:
        from .batches import finalize_batches
        finalize_batches(session, tenant_id, connection_id=connection_id)
        return stats

    connections = {
        connection.id: connection
        for connection in session.scalars(
            select(IntegrationConnection).where(IntegrationConnection.id.in_({raw.connection_id for raw in raws}))
        )
    }
    grouped: dict[uuid.UUID, dict[str, list[RawRecord]]] = defaultdict(lambda: defaultdict(list))
    for raw in raws:
        grouped[raw.connection_id][raw.stream].append(raw)

    master_changed = False
    now = utcnow()
    for conn_id, by_stream in grouped.items():
        connection = connections[conn_id]
        checker = QualityChecker(session, tenant_id, now=now, source_system=connection.source_system)
        naive_tz = str((connection.config or {}).get("naive_timezone") or "UTC")
        ctx = MappingContext.load(session, tenant_id, connection.source_system, naive_tz)
        ctx.platform = {"mock_amazon": "amazon_sp", "mock_tiktok": "tiktok_shop"}.get(connection.kind, connection.kind)
        for unknown in sorted(set(by_stream) - set(STREAMS)):
            for raw in by_stream[unknown]:
                stats.processed += 1
                stats.quarantined += 1
                _quarantine(session, raw, "unknown_stream", f"stream '{unknown}' has no canonical template")
                raw.normalize_status = "quarantined"
                raw.normalized_at = now
        for stream_name in ordered_streams(set(by_stream) & set(STREAMS)):
            stream_stats = stats.by_stream.setdefault(stream_name, {"normalized": 0, "quarantined": 0, "changed": 0})
            spec = STREAMS[stream_name]
            rows: list[CanonicalRow] = []
            accepted: list[RawRecord] = []
            market_changes = []
            for raw in by_stream[stream_name]:
                stats.processed += 1
                try:
                    if stream_name == 'market_signals' and any(k in raw.payload for k in ('received_at', 'available_at')):
                        raise _QualityRejected('reserved_market_timestamp', 'received_at/available_at are assigned by the server')
                    record = parse_record(spec, raw.payload, naive_tz)
                    row = build_row(
                        stream_name, record, ctx, raw_id=raw.id, source_updated_at=raw.source_updated_at
                    )
                    if stream_name == "inventory_snapshots":
                        batch_id = session.scalar(select(BatchRecord.batch_id)
                            .join(IngestionBatch, IngestionBatch.id == BatchRecord.batch_id)
                            .where(BatchRecord.raw_record_id == raw.id,
                                   IngestionBatch.status.in_(("landed", "complete", "quarantined")))
                            .order_by(IngestionBatch.created_at.desc()).limit(1))
                        row.values["ingestion_batch_id"] = batch_id
                    issue = checker.check(stream_name, record, row.values)
                    if issue is not None and not (issue.overridable and raw.quality_override):
                        raise _QualityRejected(issue.code, issue.detail)
                    if stream_name == 'market_signals':
                        from .market import append_revision
                        try:
                            if append_revision(session, tenant_id, connection.source_system, raw, row.values, now):
                                market_changes.append(row.key)
                        except ValueError as exc:
                            raise _QualityRejected('market_revision_conflict', str(exc)) from exc
                except FieldError as exc:
                    _quarantine(session, raw, "invalid_field", str(exc))
                    raw.normalize_status = "quarantined"
                except MappingIssue as exc:
                    _quarantine(session, raw, exc.reason_code, exc.detail)
                    raw.normalize_status = "quarantined"
                except _QualityRejected as exc:
                    _quarantine(session, raw, exc.code, exc.detail)
                    raw.normalize_status = "quarantined"
                else:
                    if stream_name != 'market_signals':
                        rows.append(row)
                    accepted.append(raw)
                    continue
                stats.quarantined += 1
                stream_stats["quarantined"] += 1
                raw.normalized_at = now
                raw.mapper_version = MAPPER_VERSION

            result = UpsertResult(len(market_changes), market_changes) if stream_name == 'market_signals' else upsert_rows(
                session,
                tenant_id=tenant_id,
                source_system=connection.source_system,
                stream=stream_name,
                rows=rows,
                ctx=ctx,
            )
            for raw in accepted:
                raw.normalize_status = "ok"
                raw.normalized_at = now
                raw.mapper_version = MAPPER_VERSION
            stats.normalized += len(accepted)
            stats.changed += result.changed
            stream_stats["normalized"] += len(accepted)
            stream_stats["changed"] += result.changed
            if result.changed:
                outbox.emit(
                    session,
                    tenant_id=tenant_id,
                    topic="canonical.changed",
                    aggregate_type=stream_name,
                    aggregate_id=conn_id,
                    payload={
                        "stream": stream_name,
                        "connection_id": conn_id,
                        "changed": result.changed,
                        "demand_days_rebuilt": result.demand_days_rebuilt,
                    },
                )
                if stream_name in MASTER_STREAMS:
                    master_changed = True
                    ctx.refresh(session)
        ctx.flush_pending(session)

    session.flush()
    if master_changed:
        stats.replayed = replay_quarantine(
            session, tenant_id, reason_codes=UNMAPPED_REASONS, resolved_by="system:master-data-changed"
        )
    from .batches import finalize_batches
    finalize_batches(session, tenant_id, connection_id=connection_id)
    return stats


class _QualityRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def replay_quarantine(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    quarantine_ids: Iterable[uuid.UUID] | None = None,
    reason_codes: Iterable[str] | None = None,
    connection_id: uuid.UUID | None = None,
    stream: str | None = None,
    resolved_by: str,
    accept_quality: bool = False,
) -> int:
    """Send open quarantine records back to normalisation (raw is the source of truth).

    ``accept_quality`` marks the raw record as operator-approved so overridable quality rules
    (demand spike, zero price) no longer reject it.
    """

    query = select(QuarantineRecord).where(
        QuarantineRecord.tenant_id == tenant_id, QuarantineRecord.status == "open"
    )
    if quarantine_ids is not None:
        ids = list(quarantine_ids)
        if not ids:
            return 0
        query = query.where(QuarantineRecord.id.in_(ids))
    if reason_codes is not None:
        query = query.where(QuarantineRecord.reason_code.in_(list(reason_codes)))
    if connection_id is not None:
        query = query.where(QuarantineRecord.connection_id == connection_id)
    if stream is not None:
        query = query.where(QuarantineRecord.stream == stream)
    records = session.scalars(query.with_for_update(skip_locked=True)).all()
    if not records:
        return 0
    now = utcnow()
    raw_ids = sorted({record.raw_record_id for record in records})
    values: dict = {"normalize_status": "pending", "normalized_at": None}
    if accept_quality:
        values["quality_override"] = True
    session.execute(
        update(RawRecord)
        .where(RawRecord.id.in_(raw_ids), RawRecord.normalize_status == "quarantined")
        .values(**values)
    )
    for record in records:
        record.status = "resolved"
        record.resolved_at = now
        record.resolved_by = resolved_by
    return len(records)


def ignore_quarantine(
    session: Session, tenant_id: uuid.UUID, quarantine_ids: Iterable[uuid.UUID], *, resolved_by: str
) -> int:
    ids = list(quarantine_ids)
    records = session.scalars(
        select(QuarantineRecord)
        .where(
            QuarantineRecord.tenant_id == tenant_id,
            QuarantineRecord.status == "open",
            QuarantineRecord.id.in_(ids),
        )
        .with_for_update()
    ).all()
    now = utcnow()
    session.execute(
        update(RawRecord)
        .where(RawRecord.id.in_([record.raw_record_id for record in records]), RawRecord.normalize_status == "quarantined")
        .values(normalize_status="skipped", normalized_at=now)
    )
    for record in records:
        record.status = "ignored"
        record.resolved_at = now
        record.resolved_by = resolved_by
    return len(records)


def replay_raw(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    connection_id: uuid.UUID,
    stream: str | None = None,
) -> int:
    """Re-normalise every landed record of a connection (after a mapper fix). Idempotent by design."""

    query = (
        update(RawRecord)
        .where(
            RawRecord.tenant_id == tenant_id,
            RawRecord.connection_id == connection_id,
            RawRecord.normalize_status.in_(("ok", "quarantined")),
        )
        .values(normalize_status="pending", normalized_at=None)
    )
    if stream:
        query = query.where(RawRecord.stream == stream)
    affected = select(BatchRecord.batch_id).join(RawRecord, RawRecord.id == BatchRecord.raw_record_id).where(
        RawRecord.tenant_id == tenant_id, RawRecord.connection_id == connection_id)
    if stream:
        affected = affected.where(RawRecord.stream == stream)
    session.execute(update(IngestionBatch).where(IngestionBatch.id.in_(affected),
        IngestionBatch.status.in_(("complete", "quarantined"))).values(status="landed", completed_at=None))
    count = session.execute(query).rowcount or 0
    resolve = update(QuarantineRecord).where(
        QuarantineRecord.tenant_id == tenant_id,
        QuarantineRecord.connection_id == connection_id,
        QuarantineRecord.status == "open",
    )
    if stream:
        resolve = resolve.where(QuarantineRecord.stream == stream)
    session.execute(resolve.values(status="resolved", resolved_at=utcnow(), resolved_by="system:replay"))
    return count
