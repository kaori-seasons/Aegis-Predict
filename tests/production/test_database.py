from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from aegis.core.db import admin_session, global_session, tenant_session
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.core.errors import Conflict, InvalidInput
from aegis.integration.raw import land_batch
from aegis.integration.normalize import normalize_batch, replay_raw
from aegis.integration.spi import RawItem
from aegis.models import (AppUser, AuditLog, DailyDemand, InventorySnapshot, Location, OrderLine,
                         PlanInputBlob, PlanLine, PlanRun, PolicyVersion, RawRecord, Sku, SyncRun, Tenant, WritebackCommand)
from aegis.planning import lifecycle
from aegis.planning.assembler import assemble
from aegis.planning.policy import PlanningPolicy

pytestmark = pytest.mark.pg


def test_rls_blocks_read_write_and_pool_context_leaks(seeded, other_tenant):
    tenant = other_tenant
    owner, _, _ = seeded
    with tenant_session(owner) as s:
        sku = s.scalar(select(Sku))
        sku_id = sku.id
    with tenant_session(tenant) as s:
        assert s.get(Sku, sku_id) is None
        assert s.scalar(select(func.count()).select_from(Sku)) == 0
    with global_session() as s:
        assert s.scalar(select(func.count()).select_from(Sku)) == 0
    with pytest.raises(DBAPIError):
        with tenant_session(tenant) as s:
            s.add(Sku(tenant_id=owner, code="hack", name="bad", pack_size=1))
            s.flush()


def test_composite_fk_prevents_cross_tenant_reference(seeded, other_tenant):
    tenant = other_tenant
    owner, _, _ = seeded
    with tenant_session(owner) as s:
        sku_id = s.scalar(select(Sku.id))
    with tenant_session(tenant) as s:
        loc = Location(tenant_id=tenant, code="OWN", kind="warehouse", timezone="UTC")
        s.add(loc); s.flush(); location_id = loc.id
    with pytest.raises(IntegrityError):
        with tenant_session(tenant) as s:
            s.add(InventorySnapshot(tenant_id=tenant, sku_id=sku_id, location_id=location_id, as_of=utcnow(), on_hand=1))
            s.flush()


def test_repeat_import_and_replay_are_idempotent(seeded):
    tenant, conn, items = seeded
    with tenant_session(tenant) as s:
        before = s.scalar(select(func.count()).select_from(DailyDemand))
        landed = land_batch(s, tenant_id=tenant, connection_id=conn, sync_run_id=None, items=items)
        assert landed.landed == 0
        replay_raw(s, tenant, connection_id=conn)
        stats = normalize_batch(s, tenant)
        assert stats.changed == 0, stats.by_stream
        assert s.scalar(select(func.count()).select_from(DailyDemand)) == before


def test_out_of_order_updates_and_cancelled_sales_local_day(seeded):
    tenant, conn, _ = seeded
    now = utcnow()
    body = {"order_id": "O", "line_id": "L", "sku": "S", "location": "W", "quantity": 7,
            "status": "shipped", "ordered_at": "2026-10-15T02:00:00Z", "unit_price": 10}
    def apply(payload, updated):
        with tenant_session(tenant) as s:
            land_batch(s, tenant_id=tenant, connection_id=conn, sync_run_id=None,
                       items=[RawItem("order_lines", "O|L", payload, updated)])
            normalize_batch(s, tenant)
    apply(body, now)
    apply({**body, "quantity": 100}, now-timedelta(hours=1))
    with tenant_session(tenant) as s:
        line = s.scalar(select(OrderLine))
        assert line.quantity == 7
        day = s.scalar(select(DailyDemand).where(DailyDemand.origin == "orders"))
        assert day.demand_date.isoformat() == "2026-10-14"
    apply({**body, "status": "cancelled"}, now+timedelta(seconds=1))
    with tenant_session(tenant) as s:
        assert s.scalar(select(func.sum(DailyDemand.units)).where(DailyDemand.origin == "orders")) == 0


def test_quality_gate_staleness_and_missing_inventory(seeded):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        with pytest.raises(InvalidInput, match="gate"):
            assemble(s, tenant, utcnow(), PlanningPolicy(max_inventory_age_hours=0.0000001))
        assembled = assemble(s, tenant, utcnow(), PlanningPolicy(allow_stale_data=True, max_inventory_age_hours=0.0000001))
        assert assembled.stale
        assert assembled.freshness["issues"]


def test_po_transit_not_double_counted(seeded):
    from aegis.integration.batches import start_batch, finish_landing
    from aegis.integration.authority import set_authority
    from aegis.inventory.service import link_inbound
    from aegis.models import PurchaseOrderLine
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        batch = start_batch(s, tenant, conn, "purchase_orders")
        land_batch(s, tenant_id=tenant, connection_id=conn, sync_run_id=None, batch_id=batch.id,
            items=[RawItem("purchase_orders", "PO|L", {"po_id": "PO", "line_id": "L", "sku": "S", "location": "W",
                "status": "open", "qty_ordered": 20, "qty_received": 5}, utcnow())])
        finish_landing(s, batch.id)
        normalize_batch(s, tenant)
        loc = s.scalar(select(Location).where(Location.code == "W"))
        set_authority(s, system_principal(tenant), domain="inbound", location_id=loc.id, connection_id=conn,
                      stream="purchase_orders", note="PO source explicitly approved")
        with pytest.raises(InvalidInput, match="overlap is unknown"):
            assemble(s, tenant, utcnow(), PlanningPolicy())
        snap = s.scalar(select(InventorySnapshot).where(InventorySnapshot.location_id == loc.id))
        po = s.scalar(select(PurchaseOrderLine))
        link_inbound(s, system_principal(tenant), snap.id, po.id, 2, "PO shipment occupies exactly 2 transit units")
        result = assemble(s, tenant, utcnow(), PlanningPolicy())
        assert result.payload["inventory"][0]["on_order"] == 13


def make_plan(tenant):
    with tenant_session(tenant) as s:
        plan = lifecycle.request_plan(s, system_principal(tenant), mode="execution")
        plan_id = plan.id
    claim = lifecycle.claim_plan()
    assert claim.id == plan_id
    assert lifecycle.execute_plan(claim) == "pending_review"
    return plan_id


def test_plan_frozen_reproduction_approval_and_idempotent_release(seeded):
    tenant, conn, _ = seeded
    plan_id = make_plan(tenant)
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        plan = lifecycle.get_plan(s, plan_id)
        result = lifecycle.reproduce(s, plan)
        assert result["summary"] == plan.summary
        with pytest.raises(Conflict):
            lifecycle.release_plan(s, p, plan_id)
        line = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == plan_id))
        assert line.final_units > 0
        lifecycle.decide_line(s, p, plan_id, line.id, approve=True)
        lifecycle.release_plan(s, p, plan_id)
        lifecycle.release_plan(s, p, plan_id)
    with tenant_session(tenant) as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "plan.release")) == 1
        with pytest.raises(Conflict, match="active plan"):
            lifecycle.request_plan(s, p)


def test_adjustment_invalidates_approval_and_pin_survives(seeded):
    tenant, _, _ = seeded
    plan_id = make_plan(tenant)
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        line = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == plan_id))
        lifecycle.decide_line(s, p, plan_id, line.id, approve=True)
        lifecycle.adjust_line(s, p, plan_id, line.id, 15, "manual override")
        assert line.status == "proposed"
        lifecycle.terminate_plan(s, p, plan_id, action="discard", note="replan")
    new_plan = make_plan(tenant)
    with tenant_session(tenant) as s:
        row = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == new_plan))
        assert row.pinned and row.final_units == 15


def test_evidence_and_policy_are_immutable(seeded):
    tenant, _, _ = seeded
    plan_id = make_plan(tenant)
    for statement in (update(RawRecord).values(payload={}), update(PolicyVersion).values(config={}),
                      update(PlanInputBlob).values(digest="0"*64)):
        with pytest.raises(DBAPIError):
            with tenant_session(tenant) as s:
                s.execute(statement)


def test_cancel_and_expired_worker_fencing(seeded):
    tenant, _, _ = seeded
    with tenant_session(tenant) as s:
        plan = lifecycle.request_plan(s, system_principal(tenant), mode="execution"); plan_id = plan.id
    claim = lifecycle.claim_plan()
    with admin_session() as s:
        s.execute(update(PlanRun).where(PlanRun.id == plan_id).values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    with tenant_session(tenant) as s:
        with pytest.raises(Conflict, match="lease"):
            lifecycle._fence(s, claim)
    from aegis.core.config import get_settings
    with admin_session() as s:
        assert lifecycle.sweep_plan_leases(s, get_settings(), utcnow()) >= 1
    with tenant_session(tenant) as s:
        lifecycle.terminate_plan(s, system_principal(tenant), plan_id, action="cancel", note="cancel test")
        assert lifecycle.get_plan(s, plan_id).status == "cancelled"


def test_unchanged_new_version_blocks_intermediate_old_change(seeded):
    tenant, conn, _ = seeded
    now = utcnow()
    original = {"sku": "S", "name": "original", "pack_size": 5, "unit_margin": 10}
    for offset, payload in ((1, original), (3, original), (2, {**original, "name": "stale"})):
        with tenant_session(tenant) as s:
            land_batch(s, tenant_id=tenant, connection_id=conn, sync_run_id=None,
                       items=[RawItem("skus", "S", payload, now+timedelta(seconds=offset))])
            normalize_batch(s, tenant)
    with tenant_session(tenant) as s:
        row = s.scalar(select(Sku))
        assert row.name == "original"
        assert row.source_updated_at == now+timedelta(seconds=3)
