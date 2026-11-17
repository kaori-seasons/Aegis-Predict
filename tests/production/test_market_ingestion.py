from datetime import UTC, datetime, timedelta
from copy import deepcopy
import csv
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from aegis.core.db import tenant_session, admin_session
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.integration.raw import land_batch
from aegis.integration.spi import RawItem
from aegis.integration.normalize import normalize_batch, replay_raw
from aegis.models import (
    MarketSignalRevision,
    RawRecord,
    QuarantineRecord,
    PlanRun,
    PlanLine,
)
from aegis.planning import lifecycle
from aegis.planning.assembler import assemble
from aegis.planning.policy import PlanningPolicy
from aegis.planning.forecast import MARKET_MODEL_VERSION
from test_workspace import login

pytestmark = pytest.mark.pg


def body(revision=1, **changes):
    now = utcnow()
    return {
        "sku": "S",
        "location": "W",
        "date": now.date().isoformat(),
        "platform": "amazon",
        "signal_kind": "planned",
        "revision": revision,
        "published_at": (now - timedelta(days=5)).isoformat(),
        "promotion_lift": 1.8,
        **changes,
    }


def ingest(tenant, conn, items):
    with tenant_session(tenant) as s:
        land_batch(
            s,
            tenant_id=tenant,
            connection_id=conn,
            sync_run_id=None,
            items=[
                RawItem("market_signals", str(uuid.uuid4()), row, utcnow())
                for row in items
            ],
        )
        return normalize_batch(s, tenant)


def test_append_history_duplicates_conflicts_and_server_availability(
    seeded, other_tenant
):
    tenant, conn, _ = seeded
    before = utcnow()
    b = body()
    assert ingest(tenant, conn, [b | {"revision": 2}, b]).changed == 2
    with tenant_session(tenant) as s:
        rows = list(
            s.scalars(
                select(MarketSignalRevision).order_by(MarketSignalRevision.revision)
            )
        )
        assert [r.revision for r in rows] == [1, 2]
        assert all(
            r.available_at >= max(before, r.received_at, r.published_at) for r in rows
        )
        frozen = [(r.id, r.available_at, r.content_hash) for r in rows]
        source = rows[0].source_system
    assert ingest(tenant, conn, [b]).changed == 0
    conflict = ingest(tenant, conn, [b | {"promotion_lift": 3.0}])
    assert conflict.quarantined == 1
    with tenant_session(tenant) as s:
        assert s.scalar(
            select(QuarantineRecord).where(
                QuarantineRecord.reason_code == "market_revision_conflict"
            )
        )
        rows = list(
            s.scalars(
                select(MarketSignalRevision).order_by(MarketSignalRevision.revision)
            )
        )
        assert [(r.id, r.available_at, r.content_hash) for r in rows] == frozen
        policy = PlanningPolicy(
            forecast_model_version=MARKET_MODEL_VERSION,
            market_signal_source=source,
            market_signal_platform="amazon",
            allow_stale_data=True,
        )
        payload = assemble(s, tenant, utcnow(), policy).payload
        assert len(payload["market_signal_revisions"]) == 2
        assert (
            payload["demand_history"][0]["calendar_start"] and payload["forecast_as_of"]
        )
        assert (
            assemble(s, tenant, before, policy).payload["market_signal_revisions"] == []
        )
    with tenant_session(other_tenant) as s:
        assert list(s.scalars(select(MarketSignalRevision))) == []
    for sql in (
        "UPDATE market_signal_revision SET promotion_lift=9 WHERE id=:id",
        "DELETE FROM market_signal_revision WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            with tenant_session(tenant) as s:
                s.execute(text(sql), {"id": frozen[0][0]})
    # The immutable trigger also protects owner code, independently of app-role grants.
    with pytest.raises(DBAPIError):
        with admin_session() as s:
            s.execute(
                text("UPDATE market_signal_revision SET revision=99 WHERE id=:id"),
                {"id": frozen[0][0]},
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"published_at": "2026-01-01T00:00:00"},
        {"published_at": (utcnow() + timedelta(days=2)).isoformat()},
        {"available_at": "2000-01-01T00:00:00Z"},
        {"received_at": "2000-01-01T00:00:00Z"},
        {"revision": 0},
        {"signal_kind": "unknown"},
        {"price_index": 0},
        {"promotion_lift": float("inf")},
        {"published_at": None},
        {"revision": None},
    ],
)
def test_invalid_or_forged_market_metadata_is_quarantined(seeded, changes):
    tenant, conn, _ = seeded
    # JSONB cannot encode Infinity; use the same textual value supplied by CSV/REST.
    if changes.get("promotion_lift") == float("inf"):
        changes = {"promotion_lift": "Infinity"}
    stats = ingest(tenant, conn, [body(**changes)])
    assert stats.quarantined == 1
    with tenant_session(tenant) as s:
        assert list(s.scalars(select(MarketSignalRevision))) == []


def test_mock_market_worker_api_and_frozen_reproduction(
    seeded, other_tenant, monkeypatch
):
    from aegis.integration.connections import create_connection, request_sync
    from aegis.integration.runtime import claim_sync_run, execute_sync_run
    from aegis.api.app import create_app

    tenant, _, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        conn = create_connection(
            s,
            p,
            name="market-fixture",
            kind="mock_market",
            config={"start_date": "2026-01-01", "days": 10},
        )
        assert conn.is_mock
        conn_id = conn.id
        source = conn.source_system
        request_sync(s, p, conn, streams=["market_signals"])
    assert execute_sync_run(claim_sync_run()) == "succeeded"
    with tenant_session(tenant) as s:
        rows = list(s.scalars(select(MarketSignalRevision)))
        assert len(rows) == 20
        assert all(r.available_at.date() == utcnow().date() for r in rows)
        lifecycle.create_policy(
            s,
            p,
            {
                "forecast_model_version": MARKET_MODEL_VERSION,
                "market_signal_source": source,
                "market_signal_platform": "amazon",
            },
        )
        plan = lifecycle.request_plan(s, p, horizon_days=7)
        plan_id = plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan()) == "pending_review"
    with tenant_session(tenant) as s:
        plan = s.get(PlanRun, plan_id)
        frozen = lifecycle.read_frozen(s, plan)
        # These fixture dates are outside this plan's history: no invented historical feature data.
        expected = lifecycle.reproduce(s, plan)
        digest = plan.input_digest
        line_id = s.scalar(select(PlanLine.id).where(PlanLine.plan_run_id == plan_id))
    ingest(tenant, conn_id, [body(revision=3, promotion_lift=4.0)])
    with tenant_session(tenant) as s:
        plan = s.get(PlanRun, plan_id)
        assert lifecycle.reproduce(s, plan) == expected and plan.input_digest == digest
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/data/market-signal-revisions").status_code == 401
        login(c, tenant)
        history = c.get(
            "/api/v1/data/market-signal-revisions",
            params={"source_system": source, "limit": 5},
        )
        assert history.status_code == 200 and len(history.json()) == 5
        assert (
            c.get(
                "/api/v1/data/market-signal-revisions?as_of=2026-01-01T00:00:00Z"
            ).json()
            == []
        )
        assert (
            c.get(
                "/api/v1/data/market-signal-revisions?as_of=2026-01-01T00:00:00"
            ).status_code
            == 422
        )
        result = c.get(f"/api/v1/workspace/plans/{plan_id}/evaluation").json()
        assert (
            len(result["models"]) == 13 and result["primary_model"] == "market_adaptive"
        )
        fc = c.get(
            f"/api/v1/workspace/plans/{plan_id}/forecast",
            params={"line_id": str(line_id), "horizon": 7},
        ).json()
        assert fc["forecast"]["model_version"] == MARKET_MODEL_VERSION
        login(c, other_tenant)
        assert c.get("/api/v1/data/market-signal-revisions").json() == []
        assert c.get(f"/api/v1/workspace/plans/{plan_id}/evaluation").status_code == 404


def test_csv_publication_contract_flows_through_real_parser(
    seeded, tmp_path, monkeypatch
):
    from aegis.integration.connections import create_connection, request_sync
    from aegis.integration.runtime import claim_sync_run, execute_sync_run
    from aegis.core.config import get_settings

    tenant, _, _ = seeded
    directory = tmp_path / str(tenant)
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "file_inbox_root", tmp_path)
    row = body()
    with (directory / "market_signals.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    with tenant_session(tenant) as s:
        conn = create_connection(
            s,
            system_principal(tenant),
            name="market-csv",
            kind="file_csv",
            config={"directory": str(tenant), "streams": {"market_signals": {}}},
        )
        request_sync(s, system_principal(tenant), conn)
    assert execute_sync_run(claim_sync_run()) == "succeeded"
    with tenant_session(tenant) as s:
        r = s.scalar(select(MarketSignalRevision))
        assert r and r.revision == 1 and r.promotion_lift == 1.8


def test_mock_market_cannot_enter_live_execution_sources(seeded):
    from aegis.integration.connections import create_connection
    from aegis.core.errors import Conflict

    tenant, _, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        conn = create_connection(
            s, p, name="market-is-mock", kind="mock_market", config={}
        )
        lifecycle.create_policy(
            s,
            p,
            {
                "forecast_model_version": MARKET_MODEL_VERSION,
                "market_signal_source": conn.source_system,
                "market_signal_platform": "amazon",
            },
        )
        plan = lifecycle.request_plan(s, p, horizon_days=7, mode="execution")
        plan_id = plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan()) == "pending_review"
    with tenant_session(tenant) as s:
        for line in s.scalars(select(PlanLine).where(PlanLine.plan_run_id == plan_id)):
            if line.final_units:
                lifecycle.decide_line(
                    s, p, plan_id, line.id, approve=True, note="synthetic gate check"
                )
        with pytest.raises(Conflict, match="mock and live"):
            lifecycle.release_plan(s, p, plan_id)


def test_rest_market_publication_fields_survive_projection_and_normalization(
    seeded, monkeypatch
):
    import httpx
    from aegis.integration.connectors.generic_rest import GenericRestConnector
    from aegis.integration.connections import create_connection, request_sync
    from aegis.integration.runtime import claim_sync_run, execute_sync_run
    from aegis.core.config import get_settings

    tenant, _, _ = seeded
    monkeypatch.setattr(get_settings(), "connector_allow_private_network", True)
    monkeypatch.setattr(get_settings(), "connector_allow_insecure_http", True)

    def handler(request):
        return httpx.Response(200, json={"items": [body()]})

    monkeypatch.setattr(
        GenericRestConnector,
        "_client",
        lambda self, config: httpx.Client(
            base_url=config.base_url, transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    with tenant_session(tenant) as s:
        config = {
            "base_url": "https://market.example",
            "streams": {
                "market_signals": {
                    "path": "/signals",
                    "records_path": "items",
                    "pagination": {"type": "none"},
                }
            },
        }
        conn = create_connection(
            s,
            system_principal(tenant),
            name="market-rest",
            kind="generic_rest",
            config=config,
        )
        request_sync(s, system_principal(tenant), conn)
    assert execute_sync_run(claim_sync_run()) == "succeeded"
    with tenant_session(tenant) as s:
        row = s.scalar(select(MarketSignalRevision))
        assert row and row.promotion_lift == 1.8 and row.received_at <= row.available_at
