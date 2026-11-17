from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from aegis.core.db import tenant_session
from aegis.core.tenancy import system_principal
from aegis.decision.history import checkpoint, facts, freeze_input, verified
from aegis.decision.jobs import claim, execute, request
from aegis.models import DemandObservation, ForecastJob, PlanRun
from aegis.planning.policy import PlanningPolicy

pytestmark = pytest.mark.pg


def test_pit_revisions_and_independent_job(seeded):
    tenant, _, _ = seeded
    p = system_principal(tenant)
    config = PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
    with tenant_session(tenant) as s:
        cp = checkpoint(s, tenant)
    with tenant_session(tenant) as s:
        old = facts(s, cp, "demand_observation")
        row = s.scalar(
            select(DemandObservation).order_by(DemandObservation.demand_date).limit(1)
        )
        row.units = 999
    with tenant_session(tenant) as s:
        assert facts(s, cp, "demand_observation") == old
        cached = s.scalar(
            select(DemandObservation).where(DemandObservation.units == 999)
        )
        assert cached.units == 999
        inp = freeze_input(s, p, cp, config)
        assert max(inp.payload["network"]["demand_history"][0]["daily_units"]) == 10
        run = request(
            s, p, "first", [7, 30], ["rolling-champion-v3"], config.model_dump(), cp.id
        )
        again = request(
            s, p, "first", [7, 30], ["rolling-champion-v3"], config.model_dump(), cp.id
        )
        assert run.id == again.id
        assert not list(s.scalars(select(PlanRun)))
    c = claim()
    assert c.id == run.id
    assert execute(c) == "succeeded"
    with tenant_session(tenant) as s:
        result = verified(s, s.get(ForecastJob, run.id).result_id, "forecast")
        assert result.payload["horizons"] == [7, 10, 17, 30]
        assert {"mean_28", "rolling-champion-v3", "seasonal-tsb-v2"} == set(
            result.payload["models"]
        )
        assert len(result.payload["rows"]) == 3


def test_checkpoint_excludes_uncommitted_revision(seeded):
    tenant, _, _ = seeded
    with tenant_session(tenant) as writer:
        row = writer.scalar(select(DemandObservation).limit(1))
        ident = str(row.id)
        row.units = 777
        writer.flush()
        with tenant_session(tenant) as reader:
            cp = checkpoint(reader, tenant)
    with tenant_session(tenant) as reader:
        versions = {p["id"]: p for p, _ in facts(reader, cp, "demand_observation")}
        assert versions[ident]["units"] != 777
        cp2 = checkpoint(reader, tenant)
        assert {p["id"]: p for p, _ in facts(reader, cp2, "demand_observation")}[ident][
            "units"
        ] == 777


def test_fact_permissions_server_clock_and_tenant_isolation(seeded, other_tenant):
    from sqlalchemy.exc import DBAPIError

    from aegis.models import KnowledgeCheckpoint

    tenant, _, _ = seeded
    with tenant_session(tenant) as s:
        cp = KnowledgeCheckpoint(
            tenant_id=tenant,
            captured_at=__import__("datetime").datetime(
                2000, 1, 1, tzinfo=__import__("datetime").UTC
            ),
            pg_snapshot="1:1:",
        )
        s.add(cp)
        s.flush()
        s.refresh(cp)
        assert cp.captured_at.year >= 2026
    with tenant_session(other_tenant) as s:
        assert s.get(KnowledgeCheckpoint, cp.id) is None
        assert not facts(
            s, cp, "sku"
        )  # explicit tenant + RLS, even with a stolen snapshot
    with pytest.raises(DBAPIError):
        with tenant_session(tenant) as s:
            s.execute(text("UPDATE knowledge_checkpoint SET captured_at=now()"))
    with pytest.raises(DBAPIError):
        with tenant_session(tenant) as s:
            s.execute(
                text(
                    "INSERT INTO fact_revision(tenant_id,table_name,entity_key,transaction_id,available_at,operation,payload) VALUES(:t,'sku','fake','1',now(),'INSERT','{}')"
                ),
                {"t": tenant},
            )
    with tenant_session(tenant) as s:
        cp = checkpoint(s, tenant)
        assert all(
            "secret_ciphertext" not in p and "config" not in p
            for p, _ in facts(s, cp, "integration_connection")
        )
        assert all("payload" not in p for p, _ in facts(s, cp, "raw_record"))


def test_api_job_evaluation_pending_permissions_and_fencing(seeded, other_tenant):
    from fastapi.testclient import TestClient
    from test_api import login

    from aegis.api.app import create_app

    tenant, _, _ = seeded
    with TestClient(create_app()) as c:
        login(c, tenant)
        assert (
            c.post(
                "/api/v1/decision/forecasts",
                json={"idempotency_key": "invalid", "horizons": [0]},
            ).status_code
            == 422
        )
        csrf = c.headers.pop("x-csrf-token")
        assert c.post("/api/v1/decision/checkpoints").status_code == 403
        c.headers["x-csrf-token"] = csrf
        r = c.post(
            "/api/v1/decision/forecasts",
            json={
                "idempotency_key": "api-1",
                "policy": {"allow_stale_data": True, "max_missing_demand_ratio": 0.1},
            },
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]
        assert (
            c.post(
                "/api/v1/decision/forecasts",
                json={"idempotency_key": "api-1", "horizons": [14]},
            ).status_code
            == 409
        )
        cl = claim()
        assert str(cl.id) == job_id
        assert execute(cl) == "succeeded"
        job = c.get("/api/v1/decision/forecasts/" + job_id).json()
        ev = c.post(
            "/api/v1/decision/results/" + job["result_id"] + "/evaluate", json={}
        )
        assert ev.status_code == 201, ev.text
        assert ev.json()["payload"]["rows"] == []
        assert ev.json()["payload"]["excluded"]["pending"] > 0
        assert (
            c.post(
                "/api/v1/decision/calibrations",
                json={"reference_id": job["result_id"], "model": "rolling-champion-v3"},
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/v1/decision/calibrations",
                json={
                    "reference_id": job["result_id"],
                    "model": "rolling-champion-v3",
                    "residuals": [1],
                },
            ).status_code
            == 422
        )
        login(c, other_tenant)
        assert c.get("/api/v1/decision/forecasts/" + job_id).status_code == 404
        assert (
            c.get("/api/v1/decision/artifacts/" + job["result_id"]).status_code == 404
        )
        login(c, tenant)
        r = c.post(
            "/api/v1/decision/forecasts",
            json={
                "idempotency_key": "cancel",
                "policy": {"allow_stale_data": True, "max_missing_demand_ratio": 0.1},
            },
        )
        cl = claim()
        assert str(cl.id) == r.json()["id"]
        assert (
            c.post("/api/v1/decision/forecasts/" + str(cl.id) + "/cancel").status_code
            == 200
        )
        assert execute(cl) == "failed"
        assert (
            c.get("/api/v1/decision/forecasts/" + str(cl.id)).json()["status"]
            == "cancelled"
        )


def actors(tenant):
    from dataclasses import replace

    from aegis.core.security import hash_password
    from aegis.models import AppUser

    with tenant_session(tenant) as s:
        first = s.scalar(select(AppUser))
        second = AppUser(
            tenant_id=tenant,
            email="approver@decision.test",
            password_hash=hash_password("test-password-12345"),
            display_name="Approver",
            roles=["APPROVER"],
            is_active=True,
        )
        s.add(second)
        s.flush()
        return replace(system_principal(tenant), user_id=first.id), replace(
            system_principal(tenant), user_id=second.id
        )


def make_past_fixture(s, p, config):
    """Explicit fictional issuance for exercising mature-label branches in tests.

    Product APIs cannot fabricate these prior-to-deployment issue checkpoints.
    """
    from copy import deepcopy

    from aegis.core.util import utcnow
    from aegis.decision.forecasting import predict
    from aegis.decision.history import artifact

    cp = checkpoint(s, p.tenant_id)
    inp = freeze_input(s, p, cp, config)
    data = deepcopy(inp.payload)
    history = data["network"]["demand_history"][0]
    history["daily_units"] = history["daily_units"][:35]
    history["stockout_days"] = history["stockout_days"][:35]
    history["missing_days"] = history["missing_days"][:35]
    output = predict(data, [7], ["mean_28", "seasonal-tsb-v2", "rolling-champion-v3"])
    output.update(
        input_id=str(inp.id),
        input_digest=inp.digest,
        calibration_id=None,
        as_of=(utcnow() - timedelta(days=26)).isoformat(),
        code_digest="fictional-test-only",
    )
    fc = artifact(
        s, p.tenant_id, "forecast", uuid.uuid4().hex, output, parent_id=inp.id
    )
    return fc


def test_mature_zero_labels_revisions_and_shadow_calibration(seeded):
    from aegis.decision import evaluation

    tenant, _, _ = seeded
    p = system_principal(tenant)
    config = PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
    with tenant_session(tenant) as s:
        fc = make_past_fixture(s, p, config)
    with tenant_session(tenant) as s:
        ev = evaluation.evaluate(s, p, fc.id)
        assert len(ev.payload["rows"]) == 3
        assert ev.payload["metrics"]["mean_28:7"]["mae"] == 0
        assert ev.payload["metrics"]["mean_28:7"]["skill_vs_mean28"] is None
        cal = evaluation.calibrate(s, p, fc.id, "rolling-champion-v3")
        assert not cal.payload["ready"]
        rel = evaluation.propose_release(s, p, cal.id, "seasonal-tsb-v2")
        assert rel.status == "blocked"
    with tenant_session(tenant) as s:
        for row in s.scalars(select(DemandObservation)):
            row.units = 0
    with tenant_session(tenant) as s:
        revised = evaluation.evaluate(s, p, fc.id)
        assert all(r["actual"] == 0 for r in revised.payload["rows"])
        assert revised.payload["metrics"]["mean_28:7"]["wape"] is None
        assert verified(s, ev.id).payload["rows"][0]["actual"] == 70
        assert {
            r["actual"] for r in evaluation.all_samples(s, fc.payload["scope"])
        } == {0}
    with tenant_session(tenant) as s:
        row = s.scalar(
            select(DemandObservation).where(
                DemandObservation.demand_date
                == __import__("datetime").date.fromisoformat(
                    fc.payload["rows"][0]["horizons"]["7"]["target_start"]
                )
            )
        )
        row.state = "censored"
    with tenant_session(tenant) as s:
        revised = evaluation.evaluate(s, p, fc.id)
        assert revised.payload["rows"] == []
        assert not evaluation.all_samples(
            s, fc.payload["scope"]
        )  # newer censored truth supersedes earlier usable label


def test_bom_approval_event_time_conservation_and_snapshot(seeded):
    from aegis.core.errors import Conflict, InvalidInput, PermissionDenied
    from aegis.core.util import utcnow
    from aegis.decision import bundles
    from aegis.decision.history import replay
    from aegis.models import Location, OrderLine, Sku

    tenant, _, _ = seeded
    p, a = actors(tenant)
    with tenant_session(tenant) as s:
        for code in ("KIT-A", "KIT-B"):
            s.add(Sku(tenant_id=tenant, code=code, name=code, source_system="manual"))
    with tenant_session(tenant) as s:
        before = checkpoint(s, tenant)
    with tenant_session(tenant) as s:
        d = bundles.draft(
            s,
            p,
            "KIT-A",
            [{"sku": "S", "quantity": 2}],
            utcnow().date() - timedelta(days=2),
            "virtual",
            "fixture",
        )
        with pytest.raises(PermissionDenied):
            bundles.publish(s, p, d.id, 1, "self approval rejected")
        bundles.publish(s, a, d.id, 1, "reviewed")
    with tenant_session(tenant) as s:
        d2 = bundles.draft(
            s,
            p,
            "KIT-B",
            [{"sku": "S", "quantity": 3}],
            utcnow().date() - timedelta(days=2),
            "virtual",
            "fixture",
        )
        bundles.publish(s, a, d2.id, 1, "reviewed")
        sku = s.scalar(select(Sku).where(Sku.code == "KIT-A"))
        loc = s.scalar(select(Location).where(Location.code == "W"))
        s.add(
            OrderLine(
                tenant_id=tenant,
                source_system="file_csv:test",
                external_order_id="o",
                external_line_id="1",
                sku_id=sku.id,
                location_id=loc.id,
                channel="amazon",
                quantity=4,
                status="paid",
                demand_eligible=True,
                ordered_at=utcnow() - timedelta(days=1),
            )
        )
    with tenant_session(tenant) as s:
        after = checkpoint(s, tenant)
    with tenant_session(tenant) as s:
        assert bundles.trace(s, p, before.id, "KIT-A")["forward"] is None
        assert bundles.trace(s, p, after.id, "S")["where_used"][0]["parent"] in (
            "KIT-A",
            "KIT-B",
        )
        assert (
            bundles.trace(s, p, after.id, "S", {"S": 10}, {"KIT-A": 2, "KIT-B": 2})[
                "component_usage"
            ]["S"]
            == 10
        )
        with pytest.raises(Conflict):
            bundles.trace(s, p, after.id, "S", {"S": 10}, {"KIT-A": 5, "KIT-B": 3})
        with pytest.raises(InvalidInput):
            bundles.draft(
                s,
                p,
                "S",
                [{"sku": "KIT-A", "quantity": 1}],
                utcnow().date(),
                "virtual",
                "cycle",
            )
        with replay(s, after):
            assert (
                s.scalar(
                    select(DemandObservation.units).where(
                        DemandObservation.kind == "order_lines"
                    )
                )
                == 8
            )
    with tenant_session(tenant) as s:
        line = s.scalar(select(OrderLine))
        line.demand_eligible = False
        line.status = "cancelled"
    with tenant_session(tenant) as s:
        with replay(s, after):
            assert (
                s.scalar(
                    select(DemandObservation.units).where(
                        DemandObservation.kind == "order_lines"
                    )
                )
                == 8
            )
        newer = checkpoint(s, tenant)
        with replay(s, newer):
            assert (
                s.scalar(
                    select(DemandObservation.units).where(
                        DemandObservation.kind == "order_lines"
                    )
                )
                is None
            )


def test_override_two_person_approval_freeze_expiry_and_risk_cas(seeded):
    from aegis.core.errors import Conflict, InvalidInput, PermissionDenied
    from aegis.core.util import utcnow
    from aegis.decision import collaboration, risks

    tenant, _, _ = seeded
    p, a = actors(tenant)
    config = PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
    with tenant_session(tenant) as s:
        cp = checkpoint(s, tenant)
        inp = freeze_input(s, p, cp, config)
        from aegis.decision.forecasting import predict
        from aegis.decision.history import artifact

        output = predict(inp.payload, [7, 30], ["mean_28", "rolling-champion-v3"])
        output.update(
            input_id=str(inp.id),
            input_digest=inp.digest,
            calibration_id=None,
            as_of=inp.payload["as_of"],
            code_digest="test",
        )
        fc = artifact(s, tenant, "forecast", uuid.uuid4().hex, output, parent_id=inp.id)
    start = utcnow().date() + timedelta(days=2)
    end = start + timedelta(days=3)
    with tenant_session(tenant) as s:
        d = collaboration.draft(
            s,
            p,
            fc.id,
            "rolling-champion-v3",
            "demand_forecast",
            start,
            end,
            100,
            [{"sku": "S", "warehouse_id": "W", "weight": 1}],
            utcnow() + timedelta(days=20),
            "campaign",
        )
        with pytest.raises(PermissionDenied):
            collaboration.decide(s, p, d.id, 1, "approve", "cannot self approve")
        collaboration.decide(s, a, d.id, 1, "approve", "reviewed")
        sc = collaboration.scenario(s, p, fc.id, [d.id])
        assert sc.payload["baseline_id"] == str(fc.id)
        base = next(
            r for r in fc.payload["rows"] if r["model"] == "rolling-champion-v3"
        )
        revised = next(
            r for r in sc.payload["rows"] if r["model"] == "rolling-champion-v3"
        )
        lo = (
            start
            - __import__("datetime").date.fromisoformat(
                base["horizons"]["7"]["target_start"]
            )
        ).days
        assert sum(revised["daily"][lo : lo + 4]) == pytest.approx(100)
        assert revised["daily"][:lo] == base["daily"][:lo]
        assert revised["daily"][lo + 4 :] == base["daily"][lo + 4 :]
        assert not revised["horizons"]["7"]["calibrated"]
        collaboration.decide(s, a, d.id, 2, "revoke", "campaign cancelled")
        with pytest.raises(InvalidInput):
            collaboration.scenario(s, p, fc.id, [d.id])
        case = risks.observe(s, p, "fixture", True, "e1", {"type": "stockout"})
        assert case.status == "watching"
        risks.observe(s, p, "fixture", True, "e1", {"type": "stockout"})
        assert case.payload["confirmations"] == 1
        risks.observe(s, p, "fixture", True, "e2", {"type": "stockout"})
        assert case.status == "open"
        rev = case.revision
        with pytest.raises(Conflict):
            risks.disposition(s, p, case.id, rev, "resolve", "cannot waive")
        risks.disposition(s, p, case.id, rev, "acknowledge", "checking source")
        with pytest.raises(Conflict):
            risks.disposition(s, p, case.id, rev, "investigate", "stale edit")
        risks.observe(s, p, "fixture", False, "e3", {"type": "stockout"})
        risks.disposition(
            s, p, case.id, case.revision, "resolve", "fresh evidence now clear"
        )
        assert case.status == "resolved"


def test_new_protection_plan_reproduces_and_unpromoted_execution_blocks(seeded):
    from aegis.core.errors import Conflict
    from aegis.decision.protection import freeze_decision
    from aegis.planning.lifecycle import (
        claim_plan,
        create_policy,
        execute_plan,
        reproduce,
        request_plan,
        terminate_plan,
    )

    tenant, _, _ = seeded
    p = system_principal(tenant)
    config = PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
    with tenant_session(tenant) as s:
        r = request(
            s, p, "protection", [7, 30], ["rolling-champion-v3"], config.model_dump()
        )
    cl = claim()
    assert cl.id == r.id
    assert execute(cl) == "succeeded"
    with tenant_session(tenant) as s:
        fc = s.get(ForecastJob, r.id).result_id
        newconfig = config.model_copy(
            update={
                "algorithm_version": "aegis-protection-v3",
                "forecast_result_id": str(fc),
                "forecast_model_version": "rolling-champion-v3",
            }
        )
        create_policy(s, p, newconfig.model_dump())
        plan = request_plan(s, p, mode="shadow")
    c = claim_plan()
    assert c.id == plan.id
    assert execute_plan(c) == "pending_review"
    with tenant_session(tenant) as s:
        actual = s.get(PlanRun, plan.id)
        result = reproduce(s, actual)
        assert result["algorithm_version"] == "aegis-protection-v3"
        line = result["lines"][0]
        assert line["protection_days"] == 17
        assert line["recommended_units"] % 5 == 0
        assert line["unavoidable_prearrival_units"] > 0
        assert line["unknown_inbound_units"] == 2
        with pytest.raises(Conflict):
            freeze_decision(s, p, newconfig, checkpoint(s, tenant), execution=True)
        terminate_plan(s, p, plan.id, action="discard", note="fixture cleanup")


def test_risk_unknown_is_not_healthy_and_scheduler_scores(seeded):
    from aegis.core.util import utcnow
    from aegis.decision import jobs, risks

    tenant, _, _ = seeded
    p = system_principal(tenant)
    config = PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
    with tenant_session(tenant) as s:
        fc = make_past_fixture(s, p, config)
        job = ForecastJob(
            tenant_id=tenant,
            idempotency_key="mature-schedule",
            request_hash="fixture",
            input_id=fc.parent_id,
            status="succeeded",
            horizons=[7],
            model_versions=fc.payload["models"],
            result_id=fc.id,
            evaluation_due_at=utcnow() - timedelta(seconds=1),
        )
        s.add(job)
        s.flush()
        job_id = job.id
    assert jobs.score_once() == "evaluated"
    with tenant_session(tenant) as s:
        assert s.get(ForecastJob, job_id).evaluation_due_at > utcnow()
        rows = risks.refresh(s, p, fc.id, PlanningPolicy().model_dump())
        unknown = next(r for r in rows if r.scope_key == "data_unknown")
        assert unknown.status == "open" and unknown.payload["condition"]


def test_daily_scheduler_is_idempotent_and_readiness_uses_pit(seeded):
    from aegis.decision import jobs
    from aegis.planning.lifecycle import create_policy
    from aegis.readiness import pilot_report

    tenant, _, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        create_policy(
            s,
            p,
            {
                "forecast_schedule_enabled": True,
                "forecast_schedule_hour_local": 0,
                "forecast_schedule_horizons": [7, 30],
                "allow_stale_data": True,
                "max_missing_demand_ratio": 0.1,
            },
        )
    # Other tenants' disabled policies may precede this tenant in the bounded scan.
    for _ in range(100):
        jobs.schedule_forecasts()
        with tenant_session(tenant) as s:
            if s.scalar(select(ForecastJob.id)):
                break
    else:
        pytest.fail("round-robin scheduler did not reach enabled tenant")
    for _ in range(5):
        jobs.schedule_forecasts()
    with tenant_session(tenant) as s:
        assert len(list(s.scalars(select(ForecastJob)))) == 1
        plan = pilot_report(s, tenant)
        assert plan["data_ready"]
        job = s.scalar(select(ForecastJob))
        job.status = "cancelled"
        create_policy(s, p, {})  # stop this fixture's schedule


def test_fva_uses_identical_baseline_and_actual_revision(seeded):
    from copy import deepcopy

    from aegis.decision import collaboration, evaluation
    from aegis.decision.history import artifact

    tenant, _, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        fc = make_past_fixture(
            s, p, PlanningPolicy(allow_stale_data=True, max_missing_demand_ratio=0.1)
        )
    with tenant_session(tenant) as s:
        ev = evaluation.evaluate(s, p, fc.id)
        data = deepcopy(fc.payload)
        data.update(baseline_id=str(fc.id), purchase_intents=[])
        row = next(r for r in data["rows"] if r["model"] == "rolling-champion-v3")
        row["operational_adjustment"] = True
        row["horizons"]["7"].update(
            expected=100, p50=100, p80=100, p95=100, calibrated=False
        )
        sc = artifact(s, tenant, "scenario", "fictional-fva", data, parent_id=fc.id)
        fva = collaboration.fva(s, p, sc.id, ev.id)
        assert fva.payload["baseline"]["mae"] == 0
        assert fva.payload["operational"]["mae"] == 30
        assert fva.payload["mae_value_added"] == -30
        assert collaboration.fva(s, p, sc.id, ev.id).id == fva.id


def test_model_release_separate_approval_and_rollback_state(seeded, monkeypatch):
    from aegis.core.errors import PermissionDenied
    from aegis.decision import evaluation, jobs
    from aegis.decision.history import artifact

    tenant, _, _ = seeded
    p, a = actors(tenant)
    # State-machine control fixture only: metric computation is independently
    # tested with500+ paired samples; public callers cannot supply this report.
    monkeypatch.setattr(
        evaluation, "promotion_report", lambda *args: {"eligible": True, "reasons": []}
    )
    with tenant_session(tenant) as s:
        cal = artifact(
            s,
            tenant,
            "calibration",
            "state-machine-fixture",
            {
                "scope": "s",
                "model": "rolling-champion-v3",
                "code_digest": jobs.code_digest(),
            },
        )
        release = evaluation.propose_release(s, p, cal.id, "seasonal-tsb-v2")
        with pytest.raises(PermissionDenied):
            evaluation.decide_release(
                s, p, release.id, 1, "activate", "self approval forbidden"
            )
        evaluation.decide_release(
            s, a, release.id, 1, "activate", "independent review fixture"
        )
        assert release.status == "active" and release.approved_by == a.user_id
        evaluation.decide_release(
            s, a, release.id, 2, "rollback", "state machine rollback fixture"
        )
        assert release.status == "rolled_back"
