from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aegis.api.administration import create_member
from aegis.api.app import create_app
from aegis.api.commerce import RevisionBody, write_revision
from aegis.core.db import tenant_session
from aegis.core.errors import Conflict, InvalidInput
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.decision import experts, jobs
from aegis.decision.history import checkpoint, verified
from aegis.models import (
    AppUser,
    DecisionCase,
    ExpertResponse,
    ForecastJob,
    IntegrationConnection,
    Tenant,
)
from aegis.planning.policy import PlanningPolicy

pytestmark = pytest.mark.pg
PASSWORD = "expert-password-12345"


@pytest.fixture
def prepared(seeded):
    tenant, cid, _ = seeded
    start = utcnow().date() + timedelta(days=3)
    with tenant_session(tenant) as s:
        user = s.scalar(select(AppUser))
        p = replace(system_principal(tenant), user_id=user.id)
        source = s.get(IntegrationConnection, cid).source_system
        write_revision(
            s,
            p,
            "sku_launches",
            RevisionBody(
                source_system=source,
                expected_revision=0,
                record={
                    "sku": "S",
                    "channel": "*",
                    "launch_group": "new-entity",
                    "cohort_key": "basic",
                    "event_kind": "planned_launch",
                    "date": str(start),
                    "note": "Known approved planned launch",
                },
            ),
        )
        write_revision(
            s,
            p,
            "launch_profiles",
            RevisionBody(
                source_system=source,
                expected_revision=0,
                record={
                    "sku": "S",
                    "location": "W",
                    "channel": "*",
                    "positioning": "Basic product",
                    "retail_price": 20,
                    "currency": "USD",
                    "price_basis": "tax_inclusive",
                    "pricing_strategy": "consumer reference",
                    "initial_supply_units": 3000,
                    "first_batch_scope": "warehouse_channel",
                },
            ),
        )
        people = [
            create_member(
                s,
                p,
                email=f"expert{i}@test.local",
                password=PASSWORD,
                roles=["FORECAST_EXPERT"],
                display_name=f"Expert {i}",
            )
            for i in range(3)
        ]
        approver = create_member(
            s, p, email="approver@test.local", password=PASSWORD, roles=["APPROVER"]
        )
        policy = PlanningPolicy(
            allow_stale_data=True,
            max_missing_demand_ratio=0.1,
            launch_signal_source=source,
            launch_profile_source=source,
        )
        slug = s.get(Tenant, tenant).slug
    with tenant_session(tenant) as s:
        job = jobs.request(
            s, p, "expert-fixture", [60], ["rolling-champion-v3"], policy.model_dump()
        )
    claimed = jobs.claim()
    assert claimed.id == job.id and jobs.execute(claimed) == "succeeded"
    with tenant_session(tenant) as s:
        fc = s.get(ForecastJob, job.id).result_id
    return {
        "tenant": tenant,
        "p": p,
        "start": start,
        "participants": [u.id for u in people],
        "approver": approver.id,
        "forecast": fc,
        "slug": slug,
        "source": source,
    }


def create_study(x):
    with tenant_session(x["tenant"]) as s:
        r = experts.create(
            s,
            x["p"],
            "test-study",
            x["forecast"],
            "rolling-champion-v3",
            "S",
            "W",
            x["start"],
            x["participants"],
        )
        return r.id


def login(client, slug, email="admin@test.local", password="test-password-12345"):
    response = client.post(
        "/api/v1/auth/login",
        json={"tenant": slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


def judgment(central=100, round_number=1):
    return {
        "round_number": round_number,
        "low": central - 10,
        "central": central,
        "high": central + 20,
        "additional_supply_units": 20,
        "rationale": "Compare age-aligned history and missing information",
        "reason_codes": ["analogue_history", "missing_data"],
        "missing_information": "Need verified retail positioning",
        "revision_reason": "Reassessed anonymous evidence" if round_number > 1 else "",
    }


def test_http_sealed_rounds_privacy_supply_invariance_approval_and_withdrawal(
    prepared, monkeypatch
):
    x = prepared
    ident = create_study(x)
    with TestClient(create_app()) as organizer:
        page = organizer.get("/expert")
        assert (
            page.status_code == 200
            and "__EXPERT_" not in page.text
            and "expert.js?v=" in page.text
        )
        assert "__EXPERT_" not in organizer.get("/").text
        login(organizer, x["slug"])
        r = organizer.get(f"/api/v1/expert-studies/{ident}").json()
        assert (
            r["participant_count"] == 3
            and r["feedback"] == []
            and r["submitted_count"] == 0
        )
        assert "initial_supply_units" not in r["packet"]["target"]["profile"]
        assert (
            organizer.post(
                f"/api/v1/expert-studies/{ident}/actions/close",
                json={"expected_revision": r["revision"], "note": "too early"},
            ).status_code
            == 409
        )
        for number in (1, 2):
            for i in range(3):
                with TestClient(create_app()) as expert:
                    login(expert, x["slug"], f"expert{i}@test.local", PASSWORD)
                    assert expert.get("/api/v1/workspace/overview").status_code == 403
                    assert (
                        expert.get(
                            f"/api/v1/decision/artifacts/{x['forecast']}"
                        ).status_code
                        == 403
                    )
                    assert (
                        expert.get(f"/api/v1/expert-studies/{ident}").status_code == 403
                    )
                    question = expert.get(f"/api/v1/expert-inbox/{ident}")
                    assert question.status_code == 200, question.text
                    q = question.json()
                    assert (
                        "participants" not in q
                        and "baseline_id" not in q
                        and "result" not in q
                    )
                    assert len(q["feedback"]) == number - 1
                    body = judgment(100 + i * 10, number)
                    first = expert.post(
                        f"/api/v1/expert-inbox/{ident}/responses", json=body
                    )
                    assert first.status_code == 201, first.text
                    assert (
                        expert.post(
                            f"/api/v1/expert-inbox/{ident}/responses", json=body
                        ).status_code
                        == 201
                    )
                    assert (
                        expert.post(
                            f"/api/v1/expert-inbox/{ident}/responses",
                            json={**body, "central": body["central"] + 1},
                        ).status_code
                        == 409
                    )
                    expert.headers.pop("x-csrf-token")
                    assert (
                        expert.post(
                            f"/api/v1/expert-inbox/{ident}/responses", json=body
                        ).status_code
                        == 403
                    )
            r = organizer.get(f"/api/v1/expert-studies/{ident}").json()
            assert r["submitted_count"] == 3
            assert (
                len(r["feedback"]) == number - 1
            )  # organizer also cannot read the open round.
            response = organizer.post(
                f"/api/v1/expert-studies/{ident}/actions/close",
                json={
                    "expected_revision": r["revision"],
                    "note": "All judgments sealed",
                },
            )
            assert response.status_code == 200, response.text
            r = response.json()
            assert (
                r["feedback"][-1]["central_median"] == 110
                and len(r["feedback"][-1]["anonymous_judgments"]) == 3
            )
            assert "additional_supply_units" not in str(r["feedback"])
            assert "expert0@test.local" not in str(r["feedback"]) and str(
                x["participants"][0]
            ) not in str(r["feedback"])
            if number == 1:
                assert (
                    organizer.post(
                        f"/api/v1/expert-studies/{ident}/actions/finalize",
                        json={
                            "expected_revision": r["revision"],
                            "note": "single round rejected",
                        },
                    ).status_code
                    == 409
                )
                response = organizer.post(
                    f"/api/v1/expert-studies/{ident}/actions/next",
                    json={
                        "expected_revision": r["revision"],
                        "note": "Consider the anonymous reasons without procurement terms",
                    },
                )
                assert response.status_code == 200, response.text
        response = organizer.post(
            f"/api/v1/expert-studies/{ident}/actions/finalize",
            json={
                "expected_revision": r["revision"],
                "note": "Two complete rounds; preserve dissent",
            },
        )
        assert response.status_code == 200, response.text
        r = response.json()
        assert r["result"]["demand"]["central_median"] == 110
        demand_result = r["result_id"]
        supply = {
            "currency": "CNY",
            "material_unit": "meter",
            "material_per_finished_unit": "0.5",
            "yield_fraction": "0.95",
            "confirmed_finished_units": 0,
            "capital_budget": "50000",
            "offers": [
                {
                    "minimum_order": "3000",
                    "quantity": "3000",
                    "unit_price": "12",
                    "setup_cost": "0",
                },
                {
                    "minimum_order": "6000",
                    "quantity": "6000",
                    "unit_price": "9.8",
                    "setup_cost": "1000",
                },
            ],
        }
        a = organizer.post(
            f"/api/v1/expert-studies/{ident}/supply-comparison", json=supply
        )
        assert a.status_code == 201, a.text
        a = a.json()["payload"]
        assert (
            a["fixed_demand_units"] == "110"
            and a["offers"][0]["total_cost"] == "36000.00"
            and a["offers"][0]["finished_equivalent_capacity"] == 5700
        )
        assert (
            a["offers"][1]["total_cost"] == "59800.00"
            and a["offers"][1]["over_capital_budget"] is True
        )
        supply["offers"][0]["minimum_order"] = "1"
        supply["offers"][0]["unit_price"] = "0.01"
        b = organizer.post(
            f"/api/v1/expert-studies/{ident}/supply-comparison", json=supply
        ).json()["payload"]
        assert (
            b["demand_result_digest"] == a["demand_result_digest"]
            and b["fixed_demand_units"] == a["fixed_demand_units"]
        )
        assert (
            organizer.get(f"/api/v1/expert-studies/{ident}").json()["result_id"]
            == demand_result
        )
        response = organizer.post(
            f"/api/v1/expert-studies/{ident}/draft-override",
            json={
                "expected_revision": r["revision"],
                "baseline_id": str(x["forecast"]),
                "expires_at": (utcnow() + timedelta(hours=12)).isoformat(),
                "note": "Rebase sealed demand on fresh baseline; not MOQ",
            },
        )
        assert response.status_code == 201, response.text
        override = response.json()
        assert override["payload"]["total"] == 110 and override["status"] == "draft"
        # Existing different-member approval and scenario flow remains authoritative.
        assert (
            organizer.post(
                f"/api/v1/decision/overrides/{override['id']}/approve",
                json={
                    "expected_revision": override["revision"],
                    "note": "self approval",
                },
            ).status_code
            == 403
        )
        with TestClient(create_app()) as approver:
            login(approver, x["slug"], "approver@test.local", PASSWORD)
            response = approver.post(
                f"/api/v1/decision/overrides/{override['id']}/approve",
                json={
                    "expected_revision": override["revision"],
                    "note": "Independent review of evidence",
                },
            )
            assert response.status_code == 200, response.text
        response = organizer.post(
            "/api/v1/decision/scenarios",
            json={"baseline_id": str(x["forecast"]), "override_ids": [override["id"]]},
        )
        assert response.status_code == 201, response.text
        scenario = response.json()
        assert scenario["payload"]["overrides"][0]["expert_result_id"] == demand_result
        # Consume the approved expert scenario in the real protection solver, then replay it.
        from aegis.decision.protection import freeze_decision, solve_protection

        with tenant_session(x["tenant"]) as session:
            baseline = verified(session, x["forecast"], "forecast")
            original = verified(
                session, uuid.UUID(baseline.payload["input_id"]), "input"
            )
            policy = PlanningPolicy.model_validate(
                {
                    **original.payload["policy"],
                    "algorithm_version": "aegis-protection-v3",
                    "forecast_result_id": str(x["forecast"]),
                    "forecast_model_version": "rolling-champion-v3",
                    "forecast_scenario_id": scenario["id"],
                }
            )
            frozen, decision = freeze_decision(
                session, x["p"], policy, checkpoint(session, x["tenant"])
            )
            assert decision["scenario_id"] == scenario["id"]
            first = solve_protection(frozen.payload["network"], policy, decision)
            replay = solve_protection(frozen.payload["network"], policy, decision)
            assert first == replay and first["lines"]
            with monkeypatch.context() as patch:
                from aegis.decision import protection

                patch.setattr(protection, "require_release", lambda *args: None)
                with pytest.raises(Conflict, match="uncalibrated uncertainty"):
                    protection.freeze_decision(
                        session,
                        x["p"],
                        policy,
                        checkpoint(session, x["tenant"]),
                        execution=True,
                    )
                with pytest.raises(Conflict, match="uncalibrated uncertainty"):
                    protection.check_release(session, policy, decision)
            with pytest.raises(Conflict):
                freeze_decision(
                    session,
                    x["p"],
                    policy,
                    checkpoint(session, x["tenant"]),
                    execution=True,
                )

        latest = organizer.get(f"/api/v1/expert-studies/{ident}").json()
        response = organizer.post(
            f"/api/v1/expert-studies/{ident}/actions/withdraw",
            json={
                "expected_revision": latest["revision"],
                "note": "Source evidence later invalidated",
            },
        )
        assert response.status_code == 200, response.text
        assert (
            organizer.post(
                "/api/v1/decision/scenarios",
                json={
                    "baseline_id": str(x["forecast"]),
                    "override_ids": [override["id"]],
                },
            ).status_code
            == 422
        )
    with tenant_session(x["tenant"]) as s:
        assert s.get(DecisionCase, uuid.UUID(override["id"])).status == "revoked"
        with pytest.raises(Conflict):
            freeze_decision(s, x["p"], policy, checkpoint(s, x["tenant"]))


def test_append_only_responses_role_separation_and_cross_tenant(prepared, other_tenant):
    x = prepared
    ident = create_study(x)
    p = replace(system_principal(x["tenant"]), user_id=x["participants"][0])
    with tenant_session(x["tenant"]) as s:
        payload = judgment()
        payload.pop("round_number")
        response = experts.submit(s, p, ident, 1, payload)
        response_id = response.id
        with pytest.raises(InvalidInput):
            create_member(
                s,
                x["p"],
                email="mixed@test.local",
                password=PASSWORD,
                roles=["PLANNER", "FORECAST_EXPERT"],
            )
    with tenant_session(other_tenant) as s:
        assert s.get(ExpertResponse, response_id) is None
        with pytest.raises(Exception) as err:
            experts.study(s, ident)
        assert "not found" in str(err.value)
    for query in (
        "UPDATE expert_response SET payload='{}'::jsonb",
        "DELETE FROM expert_response",
    ):
        with pytest.raises(DBAPIError), tenant_session(x["tenant"]) as s:
            s.execute(text(query))


def test_refresh_round_uses_new_frozen_evidence_without_overwriting_prior(prepared):
    x = prepared
    ident = create_study(x)
    with tenant_session(x["tenant"]) as s:
        original = experts.study(s, ident).payload["rounds"][0]["packet_id"]
        for member in x["participants"]:
            payload = judgment()
            payload.pop("round_number")
            experts.submit(s, replace(x["p"], user_id=member), ident, 1, payload)
        r = experts.advance(s, x["p"], ident, 1, "close", "all complete")
    with tenant_session(x["tenant"]) as s:
        write_revision(
            s,
            x["p"],
            "launch_profiles",
            RevisionBody(
                source_system=x["source"],
                expected_revision=1,
                record={
                    "sku": "S",
                    "location": "W",
                    "channel": "*",
                    "positioning": "Updated verified positioning",
                    "retail_price": 25,
                    "currency": "USD",
                    "price_basis": "tax_inclusive",
                    "pricing_strategy": "consumer market price",
                },
            ),
        )
    with tenant_session(x["tenant"]) as s:
        r = experts.advance(
            s,
            x["p"],
            ident,
            r.revision,
            "next",
            "Corrected factual consumer price",
            refresh=True,
        )
        fresh = verified(s, uuid.UUID(r.payload["rounds"][-1]["packet_id"]), "input")
        old = verified(s, uuid.UUID(original), "input")
        assert (
            fresh.payload["target"]["profile"]["retail_price"] == 25
            and old.payload["target"]["profile"]["retail_price"] == 20
        )
        assert fresh.payload["as_of"] > old.payload["as_of"]
        assert (
            fresh.payload["organizer_clarification"]
            == "Corrected factual consumer price"
        )
        with pytest.raises(Conflict):
            experts.advance(s, x["p"], ident, 1, "close", "stale revision")


def test_disagreement_must_be_acknowledged_and_expired_submissions_block(
    prepared, monkeypatch
):
    x = prepared
    ident = create_study(x)
    with tenant_session(x["tenant"]) as s:
        for number in (1, 2):
            for member, central in zip(x["participants"], (100, 1000, 10000)):
                answer = judgment(central, number)
                answer.pop("round_number")
                experts.submit(
                    s, replace(x["p"], user_id=member), ident, number, answer
                )
            r = experts.study(s, ident)
            experts.advance(
                s, x["p"], ident, r.revision, "close", "Keep all disagreements"
            )
            if number == 1:
                experts.advance(
                    s, x["p"], ident, r.revision, "next", "Review anonymously"
                )
        with pytest.raises(Conflict, match="disagreement"):
            experts.advance(
                s, x["p"], ident, r.revision, "finalize", "No acknowledgment"
            )
        r = experts.advance(
            s,
            x["p"],
            ident,
            r.revision,
            "finalize",
            "Explicitly retain uncertainty",
            acknowledge=True,
        )
        result = verified(s, uuid.UUID(r.payload["result_id"]), "input")
        assert result.payload["acknowledged_disagreement"] is True
        assert result.payload["demand"]["stable"] is False
    with tenant_session(x["tenant"]) as s:
        other = experts.create(
            s,
            x["p"],
            "expiry-case",
            x["forecast"],
            "rolling-champion-v3",
            "S",
            "W",
            x["start"],
            x["participants"],
        )
        import datetime

        monkeypatch.setattr(
            experts,
            "utcnow",
            lambda: datetime.datetime.combine(
                x["start"] + timedelta(days=1), datetime.time.min, datetime.UTC
            ),
        )
        answer = judgment()
        answer.pop("round_number")
        with pytest.raises(Conflict, match="expired"):
            experts.submit(
                s, replace(x["p"], user_id=x["participants"][0]), other.id, 1, answer
            )
        assert not experts.round_rows(s, other)


def test_launch_profile_mock_normalization_immutability_and_asof(
    prepared, other_tenant
):
    from aegis.decision.history import facts
    from aegis.integration.connections import build_ctx, create_connection
    from aegis.integration.connectors.mock_commerce import MockCommerceConnector
    from aegis.integration.normalize import normalize_batch
    from aegis.integration.raw import land_batch
    from aegis.integration.spi import RawItem
    from aegis.models import LaunchProfileRevision

    x = prepared
    with tenant_session(x["tenant"]) as s:
        cp = checkpoint(s, x["tenant"])
    with tenant_session(x["tenant"]) as s:
        connection = create_connection(
            s,
            x["p"],
            name="Expert profile mock",
            kind="mock_commerce",
            config={
                "sku": "S",
                "location_code": "W",
                "channel": "*",
                "start_date": str(utcnow().date() - timedelta(days=30)),
            },
        )
        connector = MockCommerceConnector()
        assert "launch_profiles" in connector.streams(connection.config)
        rows = [
            r
            for r in connector.read(
                build_ctx(connection), "launch_profiles", None, None
            )
            if isinstance(r, RawItem)
        ]
        assert len(rows) == 1
        before = utcnow()
        land_batch(
            s,
            tenant_id=x["tenant"],
            connection_id=connection.id,
            sync_run_id=None,
            items=rows,
        )
        result = normalize_batch(s, x["tenant"], connection_id=connection.id)
        assert result.normalized == 1 and result.quarantined == 0
        row = s.scalar(
            select(LaunchProfileRevision).where(
                LaunchProfileRevision.source_system == connection.source_system
            )
        )
        ident = row.id
        assert (
            row.received_at >= before - timedelta(seconds=1)
            and row.published_at < before
        )
        assert row.available_at >= row.received_at
    with tenant_session(x["tenant"]) as s:
        assert str(ident) not in {
            r["id"] for r, _ in facts(s, cp, "launch_profile_revision")
        }
    with tenant_session(other_tenant) as s:
        assert s.get(LaunchProfileRevision, ident) is None
    for query in (
        "UPDATE launch_profile_revision SET retail_price=1",
        "DELETE FROM launch_profile_revision",
    ):
        with pytest.raises(DBAPIError), tenant_session(x["tenant"]) as s:
            s.execute(text(query))
