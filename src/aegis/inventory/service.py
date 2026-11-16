from __future__ import annotations
import uuid
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.tenancy import Principal
from ..core.util import utcnow
from ..models import (IngestionBatch, InventoryInboundLink, InventoryReservation, InventorySnapshot,
                      PlanLine, PurchaseOrderLine, SourceAuthority, IntegrationConnection)


def link_inbound(session: Session, principal: Principal, snapshot_id: uuid.UUID,
                 purchase_order_line_id: uuid.UUID, units: int, evidence: str) -> InventoryInboundLink:
    snapshot = session.get(InventorySnapshot, snapshot_id, with_for_update=True)
    po = session.get(PurchaseOrderLine, purchase_order_line_id, with_for_update=True)
    if snapshot is None or po is None or snapshot.tenant_id != principal.tenant_id or po.tenant_id != principal.tenant_id:
        raise InvalidInput("inbound link references are unavailable")
    if (snapshot.sku_id, snapshot.location_id) != (po.sku_id, po.location_id):
        raise InvalidInput("inbound link must reference the same SKU and destination")
    if not evidence.strip() or units <= 0:
        raise InvalidInput("positive quantity and evidence required")
    existing = session.scalar(select(InventoryInboundLink).where(InventoryInboundLink.snapshot_id == snapshot.id,
                               InventoryInboundLink.purchase_order_line_id == po.id))
    snapshot_total = session.scalar(select(func.coalesce(func.sum(InventoryInboundLink.units), 0))
                                   .where(InventoryInboundLink.snapshot_id == snapshot.id))
    if snapshot_total - (existing.units if existing else 0) + units > snapshot.in_transit or units > po.qty_ordered-po.qty_received:
        raise Conflict("inbound attribution exceeds snapshot transit or PO outstanding quantity")
    row = existing or InventoryInboundLink(tenant_id=principal.tenant_id, snapshot_id=snapshot.id, purchase_order_line_id=po.id)
    row.units, row.evidence, row.created_by = units, evidence, principal.label
    session.add(row); session.flush()
    record_audit(session, principal, "inventory.inbound.link", "inventory_inbound_link", row.id,
                 after={"snapshot_id": snapshot_id, "po_line_id": po.id, "units": units, "evidence": evidence})
    return row


def on_order_units(session: Session, snapshot: InventorySnapshot, orders: list[PurchaseOrderLine], mode: str = "linked") -> int:
    outstanding = sum(po.qty_ordered-po.qty_received for po in orders)
    if not outstanding or not snapshot.in_transit:
        return outstanding
    if mode == "disjoint":
        return outstanding  # An explicitly approved source contract, never inferred by subtracting totals.
    links = list(session.scalars(select(InventoryInboundLink).where(InventoryInboundLink.snapshot_id == snapshot.id)))
    by_id = {po.id: po for po in orders}
    if not links:
        raise InvalidInput("PO and transit overlap is unknown; link documents or approve a disjoint source contract")
    if any(link.purchase_order_line_id not in by_id or link.units > by_id[link.purchase_order_line_id].qty_ordered-by_id[link.purchase_order_line_id].qty_received for link in links):
        raise InvalidInput("inbound attribution is stale or references a different PO scope")
    overlap = sum(link.units for link in links)
    if overlap != snapshot.in_transit:
        raise InvalidInput("transit attribution is incomplete; every transit unit needs a document link")
    return outstanding - overlap


def held_units(session: Session, tenant_id: uuid.UUID, sku_id: uuid.UUID, location_id: uuid.UUID) -> int:
    return session.scalar(select(func.coalesce(func.sum(InventoryReservation.units - InventoryReservation.released_units - InventoryReservation.settled_units), 0)).where(
        InventoryReservation.tenant_id == tenant_id, InventoryReservation.sku_id == sku_id,
        InventoryReservation.location_id == location_id)) or 0


def reserve_line(session: Session, principal: Principal, line: PlanLine, pool_id: uuid.UUID) -> InventoryReservation:
    if line.tenant_id != principal.tenant_id or line.status != "approved" or line.final_units <= 0:
        raise InvalidInput("reservation requires an approved positive plan line in this tenant")
    # Serialize all consumers of this stock key, including across plan versions.
    lock_key = (line.sku_id.int ^ pool_id.int ^ principal.tenant_id.int) % (2**63-1)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    existing = session.scalar(select(InventoryReservation).where(InventoryReservation.plan_line_id == line.id))
    if existing:
        if existing.status != "held" or existing.units != line.final_units:
            raise Conflict("reservation already exists with a different quantity or lifecycle")
        return existing
    rule = session.scalar(select(SourceAuthority).where(SourceAuthority.domain == "inventory", SourceAuthority.location_id == pool_id))
    if rule is None:
        raise InvalidInput("pool inventory authority must be configured before reserving stock")
    source = session.get(IntegrationConnection, rule.connection_id).source_system
    from ..integration.observations import latest_batch
    batch = latest_batch(session, rule)
    snapshot = session.scalar(select(InventorySnapshot).where(InventorySnapshot.sku_id == line.sku_id,
        InventorySnapshot.location_id == pool_id, InventorySnapshot.source_system == source,
        InventorySnapshot.as_of <= utcnow()).order_by(InventorySnapshot.as_of.desc()).limit(1).with_for_update())
    if snapshot is None:
        raise InvalidInput("pool stock snapshot is missing")
    if snapshot.ingestion_batch_id != batch.id:
        raise Conflict("pool stock is not covered by the latest complete inventory batch")
    if snapshot.ingestion_batch_id:
        batch = session.get(IngestionBatch, snapshot.ingestion_batch_id)
        if batch is None or batch.status != "complete":
            raise Conflict("pool inventory batch is incomplete")
    available = snapshot.on_hand - snapshot.allocated - snapshot.backorder - held_units(session, principal.tenant_id, line.sku_id, pool_id)
    if line.final_units > available:
        raise Conflict("insufficient unreserved stock; refresh and replan", code="reservation_conflict")
    row = InventoryReservation(tenant_id=principal.tenant_id, sku_id=line.sku_id, location_id=pool_id,
                               snapshot_id=snapshot.id, plan_line_id=line.id, units=line.final_units, status="held")
    session.add(row); session.flush()
    record_audit(session, principal, "inventory.reserve", "inventory_reservation", row.id,
                 after={"plan_line_id": line.id, "snapshot_id": snapshot.id, "units": row.units})
    return row
