"""Append provider revisions without overwriting history or backdating system availability."""

from __future__ import annotations
import uuid
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..core.util import content_hash
from ..models import MarketSignalRevision


def append_revision(session, tenant_id, source_system, raw, values, now):
    digest = content_hash(values)
    item = dict(
        values,
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source_system=source_system,
        raw_record_id=raw.id,
        received_at=raw.received_at,
        available_at=max(now, raw.received_at, values["published_at"]),
        content_hash=digest,
    )
    table = MarketSignalRevision.__table__
    inserted = session.scalar(
        insert(table)
        .values(item)
        .on_conflict_do_nothing(constraint="uq_market_revision_identity")
        .returning(table.c.id)
    )
    if inserted:
        return True
    # ON CONFLICT waits for a competing insert. Same revision with new content is an error,
    # including within one landing batch; it must be republished under a new revision.
    identity = (
        "sku_id",
        "location_id",
        "platform",
        "signal_kind",
        "signal_date",
        "revision",
    )
    previous = session.scalar(
        select(MarketSignalRevision.content_hash).where(
            MarketSignalRevision.tenant_id == tenant_id,
            MarketSignalRevision.source_system == source_system,
            *(getattr(MarketSignalRevision, k) == values[k] for k in identity),
        )
    )
    if previous != digest:
        raise ValueError(
            "provider revision identity already exists with different content; publish a new revision"
        )
    return False
