from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aegis.core.db import tenant_session
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.decision.history import checkpoint, facts, freeze_input
from aegis.integration.normalize import normalize_batch
from aegis.integration.raw import land_batch
from aegis.integration.spi import RawItem
from aegis.models import (
    CommercePerformanceRevision,
    PublicSignalBindingRevision,
    PublicSignalRevision,
)
from aegis.planning.policy import PlanningPolicy

pytestmark = pytest.mark.pg


def payload(**kw):
    now = utcnow()
    return {
        "sku": "S",
        "location": "W",
        "channel": "amazon",
        "metric": "sessions",
        "date": str(now.date() - timedelta(days=2)),
        "timezone": "America/Los_Angeles",
        "unit": "count",
        "state": "observed",
        "value": 100,
        "revision": 1,
        "published_at": (now - timedelta(days=1)).isoformat(),
        **kw,
    }


def ingest(s, tenant, connection, stream, rows):
    land_batch(
        s,
        tenant_id=tenant,
        connection_id=connection,
        sync_run_id=None,
        items=[
            RawItem(stream, str(i) + "-" + str(row.get("revision", 0)), row)
            for i, row in enumerate(rows)
        ],
    )
    return normalize_batch(s, tenant, connection_id=connection, limit=2000)


def test_normalization_contract_quarantine_and_server_clock(seeded):
    tenant, cid, _ = seeded
    with tenant_session(tenant) as s:
        before = utcnow()
        stats = ingest(
            s,
            tenant,
            cid,
            "commerce_performance",
            [
                payload(),
                payload(metric="orders", value=4, timezone="UTC"),
                payload(metric="units", value=1.5),
                payload(metric="ad_spend", unit="money", currency=""),
                payload(
                    metric="revenue",
                    unit="money",
                    currency="USD",
                    state="missing",
                    value=None,
                ),
                payload(
                    metric="unit_price",
                    unit="money",
                    currency="USD",
                    received_at="2000-01-01T00:00:00Z",
                ),
            ],
        )
        assert stats.normalized == 2 and stats.quarantined == 4
        r = s.scalar(
            select(CommercePerformanceRevision).where(
                CommercePerformanceRevision.metric == "sessions"
            )
        )
        assert (
            r.available_at >= r.received_at >= before - timedelta(seconds=1)
            and r.published_at < before
        )
        assert r.period_end_at.hour == 8  # LA local completed-day contract in November.
        assert (
            s.scalar(
                select(CommercePerformanceRevision).where(
                    CommercePerformanceRevision.metric == "revenue"
                )
            ).value
            is None
        )


def test_public_auto_revision_first_seen_restatement_and_immutability(
    seeded, other_tenant
):
    tenant, cid, _ = seeded
    now = utcnow()
    day = str(now.date() - timedelta(days=3))
    record = {
        "provider": "frankfurter",
        "series_key": "USD/EUR",
        "metric": "fx_rate",
        "date": day,
        "value": 0.9,
        "unit": "quote_per_base",
        "signal_kind": "observed",
        "publication_evidence": "first_seen",
        "attribution": "Frankfurter blended rates",
        "revision": 0,
        "published_at": "2000-01-01T00:00:00Z",
    }
    with tenant_session(tenant) as s:
        assert ingest(s, tenant, cid, "public_signals", [record]).changed == 1
    with tenant_session(tenant) as s:
        original = s.scalar(select(PublicSignalRevision))
        ident = original.id
        assert (
            original.published_at == original.received_at
            and original.published_at >= now
        )
        assert ingest(s, tenant, cid, "public_signals", [record]).changed == 0
        assert (
            ingest(
                s, tenant, cid, "public_signals", [{**record, "value": 0.91}]
            ).changed
            == 1
        )
    with tenant_session(tenant) as s:
        rows = list(
            s.scalars(
                select(PublicSignalRevision).order_by(PublicSignalRevision.revision)
            )
        )
        assert len(rows) == 2 and rows[1].revision == 2 and rows[0].value == 0.9
    with tenant_session(other_tenant) as s:
        assert s.get(PublicSignalRevision, ident) is None
    for command in (
        "UPDATE public_signal_revision SET value=1",
        "DELETE FROM public_signal_revision",
    ):
        with pytest.raises(DBAPIError):
            with tenant_session(tenant) as s:
                s.execute(text(command))


def test_checkpoint_preserves_contract_and_prior_versions(seeded):
    tenant, cid, _ = seeded
    p = system_principal(tenant)
    from aegis.models import IntegrationConnection

    with tenant_session(tenant) as s:
        source = s.get(IntegrationConnection, cid).source_system
        ingest(s, tenant, cid, "commerce_performance", [payload()])
    with tenant_session(tenant) as s:
        cp = checkpoint(s, tenant)
    with tenant_session(tenant) as s:
        ingest(
            s, tenant, cid, "commerce_performance", [payload(revision=2, value=9999)]
        )
    policy = PlanningPolicy(
        allow_stale_data=True,
        max_missing_demand_ratio=0.1,
        commerce_signal_source=source,
        commerce_signal_channel="amazon",
    )
    with tenant_session(tenant) as s:
        assert len(facts(s, cp, "commerce_performance_revision")) == 1
        frozen = freeze_input(s, p, cp, policy)
        assert [
            r["value"]
            for r in frozen.payload["network"]["commerce_context"][
                "commerce_performance"
            ]
        ] == [100]
        assert frozen.payload["watermarks"]["external_signal_sources"][0][
            "connection_id"
        ] == str(cid)
        with pytest.raises(ValueError):
            PlanningPolicy(commerce_signal_source=source)


def test_manual_cas_mapping_event_and_forecast_queue(seeded):
    from aegis.api.commerce import RevisionBody, write_revision
    from aegis.core.errors import Conflict
    from aegis.decision.jobs import claim, execute, request
    from aegis.models import ForecastJob, IntegrationConnection

    tenant, cid, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        source = s.get(IntegrationConnection, cid).source_system
        body = RevisionBody(
            source_system=source,
            expected_revision=0,
            record={
                "sku": "S",
                "channel": "*",
                "launch_group": "product-1",
                "cohort_key": "accessories",
                "event_kind": "first_sale",
                "date": str(utcnow().date() - timedelta(days=3)),
                "note": "Observed first paid order",
            },
        )
        assert write_revision(s, p, "sku_launches", body)["revision"] == 1
    with tenant_session(tenant) as s:
        with pytest.raises(Conflict):
            write_revision(s, p, "sku_launches", body)
    with tenant_session(tenant) as s:
        binding = RevisionBody(
            source_system=source,
            expected_revision=0,
            record={
                "sku": "S",
                "location": "W",
                "provider": "frankfurter",
                "series_key": "USD/EUR",
                "metric": "fx_rate",
                "effective_from": str(utcnow().date()),
                "note": "USD costs / EUR destination",
            },
        )
        write_revision(s, p, "public_signal_bindings", binding)
        row = s.scalar(select(PublicSignalBindingRevision))
        assert row.revision == 1
        policy = PlanningPolicy(
            allow_stale_data=True,
            max_missing_demand_ratio=0.1,
            launch_signal_source=source,
            public_signal_source=source,
        )
        job = request(
            s, p, "commerce-e2e", [7], ["commerce-coldstart-v5"], policy.model_dump()
        )
    c = claim()
    assert c.id == job.id
    assert execute(c) == "succeeded"
    with tenant_session(tenant) as s:
        from aegis.decision.history import verified

        fc = verified(s, s.get(ForecastJob, job.id).result_id, "forecast")
        assert {"mean_28", "rolling-champion-v3", "commerce-coldstart-v5"} <= set(
            fc.payload["models"]
        )
        row = next(
            r for r in fc.payload["rows"] if r["model"] == "commerce-coldstart-v5"
        )
        assert row["evidence"]["execution_blockers"] == [
            "insufficient_cold_start_support"
        ]


def test_contract_api_validation_message_save_reload_csrf_and_tenant(
    seeded, other_tenant
):
    from fastapi.testclient import TestClient

    from aegis.api.app import create_app
    from aegis.models import IntegrationConnection, Tenant

    tenant, cid, _ = seeded
    with tenant_session(tenant) as s:
        slug = s.get(Tenant, tenant).slug
        source = s.get(IntegrationConnection, cid).source_system
    with TestClient(create_app()) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "tenant": slug,
                "email": "admin@test.local",
                "password": "test-password-12345",
            },
        )
        csrf = login.json()["csrf_token"]
        client.headers["x-csrf-token"] = csrf
        response = client.post(
            "/api/v1/policies", json={"commerce_signal_source": source}
        )
        assert response.status_code == 422
        assert "source and channel" in response.json()["error"]["message"]
        response = client.post(
            "/api/v1/policies",
            json={
                "commerce_signal_source": source,
                "commerce_signal_channel": "amazon",
                "launch_signal_source": source,
                "public_signal_source": source,
            },
        )
        assert response.status_code == 201, response.text
        config = next(
            r for r in client.get("/api/v1/policies").json() if r["is_active"]
        )["config"]
        assert (
            config["commerce_signal_source"] == source
            and config["commerce_signal_channel"] == "amazon"
        )
        assert client.get("/api/v1/commerce/summary").status_code == 200
        client.headers.pop("x-csrf-token")
        assert (
            client.post(
                "/api/v1/commerce/launches",
                json={"source_system": source, "expected_revision": 0, "record": {}},
            ).status_code
            == 403
        )
        with tenant_session(other_tenant) as s:
            other_slug = s.get(Tenant, other_tenant).slug
        response = client.post(
            "/api/v1/auth/login",
            json={
                "tenant": other_slug,
                "email": "admin@test.local",
                "password": "test-password-12345",
            },
        )
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        response = client.post(
            "/api/v1/commerce/launches",
            json={"source_system": source, "expected_revision": 0, "record": {}},
        )
        assert (
            response.status_code == 422 and "active source not found" in response.text
        )
