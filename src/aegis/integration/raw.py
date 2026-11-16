from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..core.util import content_hash, json_safe
from ..models import BatchRecord, IngestionBatch, RawRecord
from .spi import RawItem


@dataclass(frozen=True)
class LandingResult:
    landed: int
    duplicates: int


def payload_hash(payload: dict) -> str:
    return content_hash(payload)


def land_batch(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    sync_run_id: uuid.UUID | None,
    items: Sequence[RawItem],
    batch_id: uuid.UUID | None = None,
) -> LandingResult:
    """Insert raw records; an identical (external_id, payload) is absorbed by the unique constraint.

    Re-delivery after a crash or an overlapping cursor window therefore never creates duplicates.
    """

    if not items:
        return LandingResult(0, 0)
    if batch_id is not None:
        batch = session.get(IngestionBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id or batch.connection_id != connection_id or batch.status != "landing":
            raise ValueError("landing batch is unavailable or belongs to a different tenant/connection")
        if any(item.stream != batch.stream for item in items):
            raise ValueError("all records must belong to the batch stream")
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        payload = json_safe(item.payload)
        # Source version is evidence too: A(t1), A(t3), B(t2) must retain the t3 watermark.
        digest = content_hash({"payload": payload, "source_updated_at": item.source_updated_at})
        key = (item.stream, item.external_id, digest)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "stream": item.stream,
                "external_id": item.external_id if len(item.external_id) <= 300 else content_hash(item.external_id),
                "payload": payload,
                "payload_hash": digest,
                "source_updated_at": item.source_updated_at,
                "source_ref": (item.source_ref or None) and item.source_ref[:500],
                "sync_run_id": sync_run_id,
                "normalize_status": "pending",
                "quality_override": False,
            }
        )
    statement = (
        insert(RawRecord)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["connection_id", "stream", "external_id", "payload_hash"])
        .returning(RawRecord.id)
    )
    landed = len(session.execute(statement).all())
    if batch_id is not None:
        keys = [(r["stream"], r["external_id"], r["payload_hash"]) for r in rows]
        raw_ids = session.scalars(select(RawRecord.id).where(RawRecord.connection_id == connection_id,
                tuple_(RawRecord.stream, RawRecord.external_id, RawRecord.payload_hash).in_(keys))).all()
        session.execute(insert(BatchRecord).values([
            {"tenant_id": tenant_id, "batch_id": batch_id, "raw_record_id": raw_id} for raw_id in raw_ids
        ]).on_conflict_do_nothing())
    return LandingResult(landed=landed, duplicates=len(items) - landed)


def pending_count(session: Session, connection_id: uuid.UUID, stream: str | None = None) -> int:
    query = select(func.count()).select_from(RawRecord).where(
        RawRecord.connection_id == connection_id, RawRecord.normalize_status == "pending"
    )
    if stream:
        query = query.where(RawRecord.stream == stream)
    return session.scalar(query) or 0
