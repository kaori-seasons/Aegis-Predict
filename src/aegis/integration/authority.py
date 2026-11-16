"""Explicit, audited source ownership. One source owns a stock location; demand can split channels."""
from __future__ import annotations
import uuid
from datetime import date
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.tenancy import Principal
from ..models import IntegrationConnection, Location, SourceAuthority

DOMAIN_STREAMS = {"demand": {"order_lines", "daily_demand"}, "inventory": {"inventory_snapshots"}, "inbound": {"purchase_orders"}}


def set_authority(session: Session, principal: Principal, *, domain: str, location_id: uuid.UUID,
                  connection_id: uuid.UUID, stream: str, note: str, channel: str = "*",
                  inbound_mode: str = "linked", coverage_start: date | None = None) -> SourceAuthority:
    if domain not in DOMAIN_STREAMS or stream not in DOMAIN_STREAMS[domain] or not note.strip():
        raise InvalidInput("authority requires a valid domain/stream and an approval reason")
    if domain != "demand" and channel != "*" or inbound_mode not in ("linked", "disjoint"):
        raise InvalidInput("invalid authority scope or inbound mode")
    if not channel or len(channel) > 80:
        raise InvalidInput("invalid authority channel")
    if stream == "order_lines" and coverage_start is None:
        raise InvalidInput("order demand authority requires an explicit coverage_start")
    connection = session.get(IntegrationConnection, connection_id)
    location = session.get(Location, location_id)
    if connection is None or location is None or connection.tenant_id != principal.tenant_id or location.tenant_id != principal.tenant_id:
        raise InvalidInput("authority references must belong to the caller's tenant")
    # Single transaction lock serializes wildcard and per-channel assignments.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": location_id.int % (2**63-1)})
    rules = list(session.scalars(select(SourceAuthority).where(SourceAuthority.location_id == location_id, SourceAuthority.domain == domain)))
    if any(r.channel != channel and (r.channel == "*" or channel == "*") for r in rules):
        raise Conflict("wildcard and individual channel ownership cannot overlap")
    row = next((r for r in rules if r.channel == channel), None)
    before = {"connection_id": row.connection_id, "stream": row.stream} if row else None
    if row is None:
        row = SourceAuthority(tenant_id=principal.tenant_id, domain=domain, location_id=location_id, channel=channel)
        session.add(row)
    row.connection_id, row.stream, row.note, row.approved_by = connection_id, stream, note, principal.label
    row.inbound_mode, row.coverage_start = inbound_mode, coverage_start
    session.flush()
    record_audit(session, principal, "data.authority.set", "source_authority", row.id, before=before,
                 after={"connection_id": connection_id, "stream": stream, "channel": channel,
                        "inbound_mode": inbound_mode, "coverage_start": coverage_start, "note": note})
    return row
