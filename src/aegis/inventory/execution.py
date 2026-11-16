"""Monotone physical execution counters; stock deductions survive until source reconciliation."""
from __future__ import annotations
import uuid
from sqlalchemy import select, text
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import content_hash, utcnow
from ..models import (ExecutionObservation, IngestionBatch, IntegrationConnection, InventoryReservation,
                      InventorySettlement, InventorySnapshot, PlanLine, PlanRun, SourceAuthority, WritebackCommand)
from ..integration.reconciliation import latest_check


def _reservation(session, principal, reservation_id):
    # Same lock order as release/close: parent plan -> reservation -> stock advisory key.
    row = session.get(InventoryReservation, reservation_id)
    if row is None or row.tenant_id != principal.tenant_id:
        raise InvalidInput('reservation unavailable')
    line = session.get(PlanLine, row.plan_line_id)
    plan = session.get(PlanRun, line.plan_run_id, with_for_update=True)
    row = session.get(InventoryReservation, reservation_id, with_for_update=True, populate_existing=True)
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': (row.sku_id.int ^ row.location_id.int ^ principal.tenant_id.int) % (2**63-1)})
    return row, line, plan


def observe(session, principal, reservation_id: uuid.UUID, *, connection_id: uuid.UUID, source_version: int,
            external_ref: str, shipped: int, received: int, cancelled: int, is_final: bool, occurred_at, evidence: str):
    row, line, plan = _reservation(session, principal, reservation_id)
    conn = session.get(IntegrationConnection, connection_id)
    if conn is None or conn.status != 'active' or not evidence.strip() or not external_ref.strip():
        raise InvalidInput('active execution source, document reference and evidence required')
    if plan.writeback_connection_id:
        if connection_id != plan.writeback_connection_id:
            raise InvalidInput('execution source differs from the approved writeback source')
        commands = list(session.scalars(select(WritebackCommand).where(WritebackCommand.plan_line_id == line.id)))
        # Split transport commands need a single explicit line-level aggregate readback contract.
        if not commands or any(c.status != 'confirmed' for c in commands):
            raise Conflict('confirm all external documents before accepting line-level execution')
        if len(commands) == 1 and commands[0].external_ref != external_ref:
            raise InvalidInput('execution reference does not match the confirmed document')
    else:
        authority = session.scalar(select(SourceAuthority).where(SourceAuthority.domain == 'inventory', SourceAuthority.location_id == row.location_id))
        if authority is None or authority.connection_id != connection_id:
            raise InvalidInput('manual execution evidence must come from the authoritative pool system')
    if source_version < 1 or source_version > 2**63-1 or occurred_at.tzinfo is None or occurred_at > utcnow():
        raise InvalidInput('released plan, positive version and nonfuture aware event time required')
    if any(type(n) is not int or n < 0 for n in (shipped, received, cancelled)) or shipped+cancelled > row.units or received > shipped:
        raise Conflict('execution quantities exceed the reservation or violate shipment/receipt conservation')
    if is_final and received+cancelled != row.units:
        raise Conflict('final execution requires every reserved unit received or cancelled')
    payload = dict(connection_id=str(connection_id), source_version=source_version, external_ref=external_ref,
                   shipped=shipped, received=received, cancelled=cancelled, is_final=is_final, occurred_at=occurred_at.isoformat())
    fingerprint = content_hash(payload)
    previous = session.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id == row.id)
                              .order_by(ExecutionObservation.source_version.desc()).limit(1))
    duplicate = session.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id == row.id, ExecutionObservation.source_version == source_version))
    if duplicate:
        if duplicate.payload_digest != fingerprint:
            raise Conflict('execution version reused with a different payload')
        return duplicate
    if plan.status != 'released' or occurred_at < plan.released_at:
        raise Conflict('execution must follow release of this plan')
    if previous and (source_version <= previous.source_version or previous.is_final or connection_id != previous.connection_id or external_ref != previous.external_ref or occurred_at < previous.occurred_at or shipped < previous.shipped or received < previous.received or cancelled < previous.cancelled):
        raise Conflict('execution identity, timestamp and cumulative counters must advance monotonically')
    event = ExecutionObservation(tenant_id=principal.tenant_id, reservation_id=row.id, connection_id=connection_id,
        source_version=source_version, external_ref=external_ref, shipped=shipped, received=received, cancelled=cancelled,
        is_final=is_final, occurred_at=occurred_at, payload_digest=fingerprint, evidence=evidence.strip())
    session.add(event)
    row.consumed_units, row.released_units = shipped, cancelled
    if shipped+cancelled == row.units:
        row.status = 'consumed' if shipped else 'released'
        row.released_at, row.release_evidence = utcnow(), evidence.strip()
    session.flush()
    record_audit(session, principal, 'inventory.execution.observe', 'execution_observation', event.id, after=payload)
    return event


def _checked_snapshot(session, row, location_id, batch_id, event, *, source: bool):
    from ..integration.observations import latest_batch
    rule = session.scalar(select(SourceAuthority).where(SourceAuthority.domain == 'inventory', SourceAuthority.location_id == location_id))
    if rule is None or latest_batch(session, rule).id != batch_id:
        raise Conflict('settlement requires the latest authoritative inventory batch')
    batch = session.get(IngestionBatch, batch_id)
    check = latest_check(session, batch_id)
    if batch.status != 'complete' or check is None or check.status != 'matched':
        raise Conflict('inventory settlement requires complete, source-reconciled stock')
    snapshot = session.scalar(select(InventorySnapshot).where(InventorySnapshot.ingestion_batch_id == batch_id,
                                InventorySnapshot.sku_id == row.sku_id, InventorySnapshot.location_id == location_id))
    if snapshot is None or snapshot.as_of < event.occurred_at:
        raise Conflict('snapshot predates execution or omits this SKU')
    counters = check.details.get('execution_refs', {}).get(event.external_ref)
    expected = {'shipped': event.shipped, 'received': event.received, 'cancelled': event.cancelled}
    if counters != expected:
        raise Conflict('source reconciliation must explicitly attest these document counters in the snapshot')
    return check


def settle(session, principal, reservation_id, *, source_batch_id, destination_batch_id=None, evidence: str):
    if not evidence.strip():
        raise InvalidInput('settlement evidence required')
    row, line, plan = _reservation(session, principal, reservation_id)
    event = session.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id == row.id)
                           .order_by(ExecutionObservation.source_version.desc()).limit(1))
    if event is None:
        raise Conflict('execution observation required before stock settlement')
    existing = session.scalar(select(InventorySettlement).where(InventorySettlement.observation_id == event.id))
    if existing:
        return existing
    source_check = _checked_snapshot(session, row, row.location_id, source_batch_id, event, source=True)
    destination_check = None
    if event.received:
        if destination_batch_id is None:
            raise Conflict('received stock requires destination snapshot reconciliation')
        destination_check = _checked_snapshot(session, row, line.location_id, destination_batch_id, event, source=False)
    row.settled_units = event.shipped
    result = InventorySettlement(tenant_id=principal.tenant_id, reservation_id=row.id, observation_id=event.id,
        source_check_id=source_check.id, destination_check_id=destination_check.id if destination_check else None, evidence=evidence.strip())
    session.add(result); session.flush()
    record_audit(session, principal, 'inventory.execution.settle', 'inventory_settlement', result.id,
                 after={'observation_id': event.id, 'shipped': event.shipped, 'received': event.received})
    return result


def assert_plan_complete(session, plan):
    rows = list(session.scalars(select(InventoryReservation).join(PlanLine, PlanLine.id == InventoryReservation.plan_line_id).where(PlanLine.plan_run_id == plan.id)))
    if not rows:
        raise Conflict('released plan is missing stock reservations')
    for row in rows:
        event = session.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id == row.id).order_by(ExecutionObservation.source_version.desc()).limit(1))
        settlement = session.scalar(select(InventorySettlement).where(InventorySettlement.observation_id == event.id)) if event else None
        if event is None or not event.is_final or settlement is None or row.consumed_units != row.settled_units or row.consumed_units+row.released_units != row.units:
            raise Conflict('every reservation needs final execution and reconciled stock settlement before close')
