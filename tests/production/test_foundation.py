from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError
from aegis.core.db import tenant_session
from aegis.core.errors import Conflict, InvalidInput
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.integration.authority import set_authority
from aegis.integration.batches import start_batch, finish_landing
from aegis.integration.connections import create_connection, request_sync, reset_stream
from aegis.integration.demand_status import eligible
from aegis.integration.normalize import normalize_batch
from aegis.integration.observations import zero_is_proven
from aegis.integration.raw import land_batch
from aegis.integration.registry import register
from aegis.integration.spi import Checkpoint, RawItem
from aegis.integration.streams import FieldError, STREAMS, parse_record
from aegis.models import (DemandObservation, IngestionBatch, IntegrationConnection, InventoryReservation,
    Location, OrderLine, PlanLine, PlanRun, PolicyVersion, RawRecord, Sku, SourceAuthority, SyncStreamState)
from aegis.planning.assembler import assemble
from aegis.planning.policy import PlanningPolicy
from aegis.planning import lifecycle


@pytest.mark.parametrize("platform,state,wanted", [
    ("amazon_sp", "PENDING", False), ("amazon_sp", "UNFULFILLABLE", False),
    ("amazon_sp", "UNSHIPPED", True), ("amazon_sp", "SHIPPED", True),
    ("tiktok_shop", "UNPAID", False), ("tiktok_shop", "ON_HOLD", False),
    ("tiktok_shop", "AWAITING_SHIPMENT", True), ("tiktok_shop", "COMPLETED", True),
    ("tiktok_shop", "CANCELLED", False)])
def test_platform_accepted_demand_contract(platform, state, wanted):
    assert eligible(platform, state) is wanted


def test_unknown_status_and_oversized_ids_fail_closed():
    with pytest.raises(FieldError): eligible("tiktok_shop", "UNKNOWN_STATE")
    with pytest.raises(FieldError, match="length"):
        parse_record(STREAMS["order_lines"], {"order_id": "x"*121, "line_id": "L", "sku": "S",
            "location": "W", "quantity": 1, "status": "shipped", "ordered_at": "2026-11-01T00:00:00Z"})
    with pytest.raises(FieldError, match="finite"):
        parse_record(STREAMS["fx_rates"], {"base": "USD", "quote": "EUR", "date": "2026-11-01", "rate": "NaN"})


def land_complete(session, tenant, connection, stream, items, **kwargs):
    batch = start_batch(session, tenant, connection, stream, **kwargs)
    land_batch(session, tenant_id=tenant, connection_id=connection, sync_run_id=None, items=items, batch_id=batch.id)
    finish_landing(session, batch.id)
    normalize_batch(session, tenant)
    return batch


def test_landing_and_partial_batches_are_never_normalized(seeded):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        batch = start_batch(s, tenant, conn, "order_lines")
        item = RawItem("order_lines", "O|L", {"order_id": "O", "line_id": "L", "sku": "S", "location": "W",
            "quantity": 10, "status": "shipped", "ordered_at": "2026-11-01T00:00:00Z"}, utcnow())
        land_batch(s, tenant_id=tenant, connection_id=conn, sync_run_id=None, items=[item], batch_id=batch.id)
        assert normalize_batch(s, tenant).processed == 0
        assert s.scalar(select(func.count()).select_from(OrderLine)) == 0
        finish_landing(s, batch.id); normalize_batch(s, tenant)
        assert batch.status == "complete"
        assert batch.record_count == 1 and len(batch.digest) == 64
        assert s.scalar(select(func.count()).select_from(OrderLine)) == 1


def test_quarantine_is_hard_gate_even_with_stale_acknowledgement(seeded):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        batch = land_complete(s, tenant, conn, "inventory_snapshots", [RawItem("inventory_snapshots", "bad", {
            "sku": "S", "location": "W", "as_of": utcnow().isoformat(), "on_hand": -1}, utcnow())])
        assert batch.status == "quarantined"
        with pytest.raises(InvalidInput, match="complete latest batch"):
            assemble(s, tenant, utcnow(), PlanningPolicy(allow_stale_data=True))


def test_complete_batch_cannot_mix_missing_location_with_old_stock(seeded):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        batch = land_complete(s, tenant, conn, "inventory_snapshots", [RawItem("inventory_snapshots", "pool-only", {
            "sku": "S", "location": "GLOBAL-POOL", "as_of": utcnow().isoformat(), "on_hand": 1000}, utcnow())])
        assert batch.status == "complete"
        with pytest.raises(InvalidInput, match="coverage is incomplete"):
            assemble(s, tenant, utcnow(), PlanningPolicy(allow_stale_data=True))


def test_authority_scope_and_tenant_cannot_overlap(seeded, other_tenant):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        loc = s.scalar(select(Location).where(Location.code == "W")); loc_id = loc.id
        with pytest.raises(Conflict, match="overlap"):
            set_authority(s, system_principal(tenant), domain="demand", location_id=loc.id,
                connection_id=conn, stream="daily_demand", channel="shop2", note="bad overlap")
    with tenant_session(other_tenant) as s:
        with pytest.raises(InvalidInput, match="references"):
            set_authority(s, system_principal(other_tenant), domain="inventory", location_id=loc_id,
                connection_id=conn, stream="inventory_snapshots", note="cross tenant")
    with pytest.raises(DBAPIError, match="overlapping source authority"):
        with tenant_session(tenant) as s:
            s.add(SourceAuthority(tenant_id=tenant, location_id=loc_id, domain="demand", channel="shop2",
                connection_id=conn, stream="daily_demand", inbound_mode="linked", note="direct SQL", approved_by="test")); s.flush()


def test_channels_do_not_add_imported_aggregate_twice(seeded):
    tenant, conn, items = seeded; p = system_principal(tenant)
    with tenant_session(tenant) as s:
        second = create_connection(s, p, name="second", kind="file_csv", config={"directory": str(tenant)})
        loc = s.scalar(select(Location).where(Location.code == "W"))
        for connection, channel, value in ((conn, "amazon:shop1", 10), (second.id, "tiktok:shop2", 5)):
            rows = [RawItem("daily_demand", item.external_id+channel, {**item.payload, "channel": channel, "units": value}, utcnow())
                    for item in items if item.stream == "daily_demand"]
            land_complete(s, tenant, connection, "daily_demand", rows)
        s.execute(delete(SourceAuthority).where(SourceAuthority.domain == "demand"))
        for connection, channel in ((conn, "amazon:shop1"), (second.id, "tiktok:shop2")):
            set_authority(s, p, domain="demand", location_id=loc.id, connection_id=connection,
                stream="daily_demand", channel=channel, note="one owner per channel")
        result = assemble(s, tenant, utcnow(), PlanningPolicy())
        series=result.payload["demand_history"][0]
        # A UTC-based fixture may leave a completed LA day unobserved; never
        # confuse a missing-day placeholder with an observed zero sale.
        observed=[u for u,missing in zip(series['daily_units'],series['missing_days']) if not missing]
        assert len(observed)==60 and set(observed)=={15}
        assert all(u==0 for u,missing in zip(series['daily_units'],series['missing_days']) if missing)
        assert s.scalar(select(func.count()).select_from(DemandObservation)) == 180


def test_missing_is_not_observed_zero(seeded):
    tenant, _, _ = seeded
    with tenant_session(tenant) as s:
        rows = list(s.scalars(select(DemandObservation).order_by(DemandObservation.demand_date)))
        s.delete(rows[15]); s.flush()
        series = assemble(s, tenant, utcnow(), PlanningPolicy()).payload["demand_history"][0]
        assert series["missing_days"][15] and not series["stockout_days"][15]
        for row in rows[20:30]: s.delete(row)
        s.flush()
        with pytest.raises(InvalidInput, match="missing demand coverage"):
            assemble(s, tenant, utcnow(), PlanningPolicy(allow_stale_data=True))


def test_zero_needs_complete_contiguous_order_windows():
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    rule = SimpleNamespace(stream="order_lines", coverage_start=datetime(2026, 1, 1).date())
    day = datetime(2026, 3, 8).date()
    start, end = datetime(2026, 3, 8, 8, tzinfo=UTC), datetime(2026, 3, 9, 7, tzinfo=UTC)
    assert zero_is_proven(rule, [(start, end)], day, ZoneInfo("America/Los_Angeles"))
    assert not zero_is_proven(rule, [(start, end-timedelta(seconds=1))], day, ZoneInfo("America/Los_Angeles"))
    assert not zero_is_proven(rule, [(start, start+timedelta(hours=1)), (start+timedelta(hours=2), end)], day, ZoneInfo("America/Los_Angeles"))


@register
class PartialSource:
    kind, version = "test-partial-source", "1"
    fail = True
    def validate_config(self, config, settings=None): return config
    def streams(self, config): return ["order_lines"]
    def read(self, ctx, stream, cursor, window):
        yield RawItem(stream, "O|L", {"order_id": "O", "line_id": "L", "sku": "S", "location": "W", "quantity": 2,
            "status": "shipped", "ordered_at": "2026-11-01T00:00:00Z"}, datetime(2026, 11, 2, tzinfo=UTC))
        yield Checkpoint(stream, {"updated_since": "2026-11-02T00:00:00Z"})
        if type(self).fail:
            from aegis.integration.errors import ContractError
            raise ContractError("page two schema changed")


def test_failed_page_keeps_cursor_and_retry_relinks_raw(seeded):
    from aegis.integration.runtime import run_sync_once
    tenant, _, _ = seeded; PartialSource.fail = True
    with tenant_session(tenant) as s:
        conn = create_connection(s, system_principal(tenant), name="partial", kind=PartialSource.kind,
            config={"initial_since": "2026-11-01T00:00:00Z"}); conn_id = conn.id
        request_sync(s, system_principal(tenant), conn)
    assert run_sync_once() == "partial"
    with tenant_session(tenant) as s:
        state = s.get(SyncStreamState, (conn_id, "order_lines"))
        assert state.cursor is None and state.status == "halted"
        assert s.scalar(select(IngestionBatch.status).where(IngestionBatch.connection_id == conn_id)) == "failed"
        assert s.scalar(select(func.count()).select_from(OrderLine)) == 0
        conn = s.get(IntegrationConnection, conn_id)
        reset_stream(s, system_principal(tenant), conn, "order_lines")
        request_sync(s, system_principal(tenant), conn)
    PartialSource.fail = False
    assert run_sync_once() == "succeeded"
    with tenant_session(tenant) as s:
        assert s.get(SyncStreamState, (conn_id, "order_lines")).cursor["updated_since"] == "2026-11-02T00:00:00Z"
        assert s.scalar(select(func.count()).select_from(OrderLine)) == 1
        assert s.scalar(select(func.count()).select_from(RawRecord).where(RawRecord.connection_id == conn_id)) == 1


def reservation_lines(tenant):
    with tenant_session(tenant) as s:
        policy = s.scalar(select(PolicyVersion).where(PolicyVersion.is_active)); sku = s.scalar(select(Sku))
        dest = s.scalar(select(Location).where(Location.code == "W")); pool = s.scalar(select(Location).where(Location.kind == "pool"))
        other = Location(tenant_id=tenant, code="W2", kind="warehouse", timezone="UTC"); s.add(other); s.flush()
        plan = PlanRun(tenant_id=tenant, status="pending_review", mode="execution", policy_version_id=policy.id,
            horizon_days=30, cutoff_at=utcnow(), seed=1, requested_by="test"); s.add(plan); s.flush()
        rows = []
        for location in (dest, other):
            line = PlanLine(tenant_id=tenant, plan_run_id=plan.id, sku_id=sku.id, location_id=location.id,
                sku_code=sku.code, location_code=location.code, status="approved", final_units=600)
            s.add(line); s.flush(); rows.append(line.id)
        return pool.id, rows


def test_concurrent_reservations_never_exceed_stock(seeded):
    from aegis.inventory.service import reserve_line
    tenant, _, _ = seeded; pool, rows = reservation_lines(tenant)
    def reserve(line_id):
        try:
            with tenant_session(tenant) as s:
                return str(reserve_line(s, system_principal(tenant), s.get(PlanLine, line_id), pool).id)
        except Conflict: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as workers: results = list(workers.map(reserve, rows))
    assert results.count("conflict") == 1
    with tenant_session(tenant) as s:
        reservation = s.scalar(select(InventoryReservation)); line = s.get(PlanLine, reservation.plan_line_id)
        assert reserve_line(s, system_principal(tenant), line, pool).id == reservation.id
        assert s.scalar(select(func.sum(InventoryReservation.units))) == 600


def test_shadow_release_and_default_writeback_are_blocked(seeded):
    from aegis.integration.writeback import claim_writeback
    tenant, _, _ = seeded; reservation_lines(tenant)
    with tenant_session(tenant) as s:
        plan = s.scalar(select(PlanRun)); plan.mode = "shadow"
        with pytest.raises(Conflict, match="shadow plans"):
            lifecycle.release_plan(s, system_principal(tenant), plan.id)
    assert claim_writeback() is None


def test_raw_evidence_cannot_be_deleted(seeded):
    tenant, _, _ = seeded
    with pytest.raises(DBAPIError, match="evidence deletion"):
        with tenant_session(tenant) as s: s.execute(delete(RawRecord))


def test_demo_cannot_be_exposed_in_production(monkeypatch):
    from fastapi.testclient import TestClient
    from overseas_inventory.competition_web import app
    monkeypatch.setenv("AEGIS_ENV", "prod")
    with TestClient(app) as client:
        assert client.get("/").status_code == 403
        assert client.get("/student/health").status_code == 403


def test_unpaid_then_paid_and_cancelled_projection_updates_without_doubling(seeded):
    from sqlalchemy import update
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        s.execute(update(IntegrationConnection).where(IntegrationConnection.id == conn).values(kind="tiktok_shop"))
        now = utcnow()
        base = {"order_id": "O", "line_id": "L", "sku": "S", "location": "W", "quantity": 3,
                "ordered_at": "2026-11-01T12:00:00Z", "channel": "tiktok:shop1:US"}
        for i, (state, wanted) in enumerate((("UNPAID", 0), ("AWAITING_SHIPMENT", 3), ("CANCELLED", 0))):
            land_complete(s, tenant, conn, "order_lines", [RawItem("order_lines", "O|L", {**base, "status": state}, now+timedelta(seconds=i))])
            assert s.scalar(select(func.sum(DemandObservation.units)).where(DemandObservation.kind == "order_lines")) == wanted
            assert s.scalar(select(func.count()).select_from(OrderLine)) == 1


def test_published_frozen_input_cannot_bypass_a_new_failed_batch(seeded):
    from aegis.models import InventorySnapshot
    tenant, conn, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        plan = lifecycle.request_plan(s, p, mode="execution"); plan_id = plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan()) == "pending_review"
    with tenant_session(tenant) as s:
        line = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == plan_id))
        lifecycle.decide_line(s, p, plan_id, line.id, approve=True)
        land_complete(s, tenant, conn, "inventory_snapshots", [RawItem("inventory_snapshots", "bad-later", {
            "sku": "S", "location": "W", "as_of": utcnow().isoformat(), "on_hand": -1}, utcnow())])
        with pytest.raises(InvalidInput, match="complete latest batch"):
            lifecycle.release_plan(s, p, plan_id, acknowledge_stale=True)
        assert s.scalar(select(func.count()).select_from(InventoryReservation)) == 0


def test_disabled_execution_and_pending_writeback_remain_blocked(seeded, monkeypatch):
    from aegis.core.config import get_settings
    from aegis.models import WritebackCommand
    from aegis.integration.writeback import claim_writeback
    tenant, conn, _ = seeded
    monkeypatch.setattr(get_settings(), "plan_execution_enabled", False)
    monkeypatch.setattr(get_settings(), "writeback_enabled", False)
    with tenant_session(tenant) as s:
        with pytest.raises(InvalidInput, match="execution plans are disabled"):
            lifecycle.request_plan(s, system_principal(tenant), mode="execution")
        command = WritebackCommand(tenant_id=tenant, connection_id=conn, command_type="create_transfer",
            idempotency_key=f"disabled-{tenant.hex}", payload={}, status="pending")
        s.add(command); s.flush(); command_id = command.id
    assert claim_writeback() is None
    with tenant_session(tenant) as s:
        command = s.get(WritebackCommand, command_id)
        assert command.status == "pending" and command.attempts == 0
        command.status = "cancelled"


def test_some_order_rows_do_not_prove_a_complete_sales_day(seeded):
    from sqlalchemy import update
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        rule = s.scalar(select(SourceAuthority).where(SourceAuthority.domain == "demand"))
        # Simulate imported order totals without a proven full search window.
        s.execute(update(DemandObservation).values(kind="order_lines"))
        rule.stream = "order_lines"
        rule.coverage_start = (utcnow()-timedelta(days=90)).date()
        batch = start_batch(s, tenant, conn, "order_lines")
        finish_landing(s, batch.id); normalize_batch(s, tenant)
        with pytest.raises(InvalidInput, match="missing demand coverage"):
            assemble(s, tenant, utcnow(), PlanningPolicy(allow_stale_data=True))


def test_spike_rule_uses_source_separated_observation_history(seeded):
    tenant, conn, _ = seeded
    with tenant_session(tenant) as s:
        batch = land_complete(s, tenant, conn, "daily_demand", [RawItem("daily_demand", "spike", {
            "sku": "S", "location": "W", "date": (utcnow().date()-timedelta(days=1)).isoformat(), "units": 1000}, utcnow())])
        assert batch.status == "quarantined"
        raw = s.scalar(select(RawRecord).where(RawRecord.external_id == "spike"))
        assert raw.normalize_status == "quarantined"
