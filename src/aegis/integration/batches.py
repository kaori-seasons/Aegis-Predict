"""A read stream is not planning evidence until all pages landed AND every member normalized."""
from __future__ import annotations
import uuid
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ..core.util import utcnow
from ..core.util import content_hash
from ..models import BatchRecord, IngestionBatch, RawRecord


def start_batch(session: Session, tenant_id: uuid.UUID, connection_id: uuid.UUID, stream: str, *,
                sync_run_id=None, window_start=None, window_end=None, scope=None) -> IngestionBatch:
    row = IngestionBatch(id=uuid.uuid4(), tenant_id=tenant_id, connection_id=connection_id, stream=stream,
                         sync_run_id=sync_run_id, status="landing", window_start=window_start,
                         window_end=window_end, scope=scope or {})
    session.add(row); session.flush()
    return row


def finish_landing(session: Session, batch_id: uuid.UUID) -> None:
    row = session.get(IngestionBatch, batch_id, with_for_update=True)
    if row.status != "landing":
        raise ValueError("only landing batches can be sealed")
    row.status, row.landed_at = "landed", utcnow()
    row.record_count = session.scalar(select(func.count()).select_from(BatchRecord).where(BatchRecord.batch_id == row.id)) or 0
    members = session.execute(select(RawRecord.stream, RawRecord.external_id, RawRecord.payload_hash)
                .join(BatchRecord, BatchRecord.raw_record_id == RawRecord.id)
                .where(BatchRecord.batch_id == row.id).order_by(RawRecord.stream, RawRecord.external_id, RawRecord.payload_hash)).all()
    row.digest = content_hash([list(member) for member in members])


def fail_batch(session: Session, batch_id: uuid.UUID, error: str) -> None:
    session.execute(update(IngestionBatch).where(IngestionBatch.id == batch_id, IngestionBatch.status != "complete")
                    .values(status="failed", error=error[:2000]))


def finalize_batches(session: Session, tenant_id: uuid.UUID, *, connection_id=None) -> int:
    session.flush()
    query = select(IngestionBatch).where(IngestionBatch.tenant_id == tenant_id,
                                        IngestionBatch.status.in_(("landed", "quarantined")))
    if connection_id:
        query = query.where(IngestionBatch.connection_id == connection_id)
    completed = 0
    for batch in session.scalars(query.with_for_update(skip_locked=True)):
        statuses = set(session.scalars(select(RawRecord.normalize_status).join(BatchRecord, BatchRecord.raw_record_id == RawRecord.id)
                                       .where(BatchRecord.batch_id == batch.id)))
        if statuses - {"ok"}:
            batch.status = "quarantined" if statuses & {"quarantined", "skipped"} else "landed"
        else:
            batch.status, batch.completed_at, batch.error = "complete", utcnow(), None
            completed += 1
    session.flush()
    return completed
