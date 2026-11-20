"""Tenant/CSRF/permission-bound APIs for forecasting and decision operations."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..core import security as sec
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput, NotFound
from ..decision import bundles, collaboration, evaluation, jobs, risks
from ..decision.history import checkpoint, verified
from ..models import (
    DecisionArtifact,
    DecisionCase,
    ForecastJob,
    KnowledgeCheckpoint,
    PolicyVersion,
)
from ..planning.policy import PlanningPolicy


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ForecastBody(Body):
    idempotency_key: str = Field(min_length=1, max_length=100)
    horizons: list[int] = Field(
        default_factory=lambda: [7, 30], min_length=1, max_length=20
    )
    models: list[
        Literal["seasonal-tsb-v2", "rolling-champion-v3", "market-aware-v4", "commerce-coldstart-v5"]
    ] = Field(
        default_factory=lambda: ["rolling-champion-v3"], min_length=1, max_length=4
    )
    policy: PlanningPolicy | None = None
    checkpoint_id: uuid.UUID | None = None
    calibration_id: uuid.UUID | None = None


class EvaluateBody(Body):
    truth_checkpoint_id: uuid.UUID | None = None


class CalibrationBody(Body):
    reference_id: uuid.UUID
    model: Literal["seasonal-tsb-v2", "rolling-champion-v3", "market-aware-v4", "commerce-coldstart-v5"]


class ReleaseBody(Body):
    calibration_id: uuid.UUID


class ActionBody(Body):
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=2000)
    assignee_id: uuid.UUID | None = None
    suppress_until: datetime | None = None


class Component(Body):
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=100000, strict=True)


class BomBody(Body):
    parent: str = Field(min_length=1, max_length=80)
    components: list[Component] = Field(min_length=1, max_length=100)
    effective_from: date
    stock_mode: Literal["virtual", "prepacked"] = "virtual"
    note: str = Field(min_length=1, max_length=2000)


class TraceBody(Body):
    input_id: uuid.UUID
    sku: str = Field(min_length=1, max_length=80)
    warehouse_id: str = Field(min_length=1, max_length=64)
    allocations: dict[str, int] = Field(default_factory=dict, max_length=100)


class Target(Body):
    sku: str = Field(min_length=1, max_length=80)
    warehouse_id: str = Field(min_length=1, max_length=64)
    weight: float = Field(ge=0, le=100000000)


class OverrideBody(Body):
    baseline_id: uuid.UUID
    model: Literal["seasonal-tsb-v2", "rolling-champion-v3", "market-aware-v4", "commerce-coldstart-v5"]
    kind: Literal["demand_forecast", "purchase_intent"]
    start: date
    end: date
    total: int = Field(ge=0, le=10000000, strict=True)
    targets: list[Target] = Field(min_length=1, max_length=100)
    expires_at: datetime
    note: str = Field(min_length=1, max_length=2000)


class ScenarioBody(Body):
    baseline_id: uuid.UUID
    override_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class RiskBody(Body):
    result_id: uuid.UUID


class FvaBody(Body):
    evaluation_id: uuid.UUID


def current_policy(session):
    row = session.scalar(select(PolicyVersion).where(PolicyVersion.is_active))
    return row.config if row else PlanningPolicy().model_dump()


def install(app, allowed, view):
    read = allowed(sec.DATA_VIEW)
    run = allowed(sec.FORECAST_RUN)

    @app.get("/api/v1/decision/assignees")
    def assignees(ctx=Depends(read)):
        from ..models import AppUser

        s, _ = ctx
        return [
            {"id": str(r.id), "label": r.display_name or r.email}
            for r in s.scalars(
                select(AppUser)
                .where(AppUser.is_active)
                .order_by(AppUser.email)
                .limit(500)
            )
        ]

    @app.get("/api/v1/decision/summary")
    def summary(ctx=Depends(read)):
        s, _ = ctx
        return {
            "jobs": [
                view(r)
                for r in s.scalars(
                    select(ForecastJob)
                    .order_by(ForecastJob.created_at.desc())
                    .limit(50)
                )
            ],
            "cases": [
                view(r)
                for kind in ("bom_draft", "override", "model_release", "risk")
                for r in s.scalars(
                    select(DecisionCase)
                    .where(DecisionCase.kind == kind)
                    .order_by(DecisionCase.updated_at.desc())
                    .limit(200)
                )
            ],
            "artifacts": [
                {
                    **view(r, exclude={"payload"}),
                    "summary": {
                        k: r.payload.get(k)
                        for k in (
                            "scope",
                            "model",
                            "ready",
                            "status",
                            "as_of",
                            "metrics",
                            "support",
                            "eligible",
                            "reasons",
                            "baseline",
                            "operational",
                            "mae_value_added",
                        )
                    },
                }
                for r in s.scalars(
                    select(DecisionArtifact)
                    .where(
                        DecisionArtifact.kind.in_(
                            (
                                "forecast",
                                "evaluation",
                                "calibration",
                                "bom",
                                "scenario",
                                "promotion_evidence",
                            )
                        )
                    )
                    .order_by(DecisionArtifact.created_at.desc())
                    .limit(100)
                )
            ],
            "checkpoints": [
                view(r)
                for r in s.scalars(
                    select(KnowledgeCheckpoint)
                    .order_by(KnowledgeCheckpoint.captured_at.desc())
                    .limit(30)
                )
            ],
            "gates": evaluation.GATES,
        }

    @app.get("/api/v1/decision/artifacts/{ident}")
    def get_artifact(ident: uuid.UUID, ctx=Depends(read)):
        s, _ = ctx
        return view(verified(s, ident))

    @app.post("/api/v1/decision/checkpoints", status_code=201)
    def capture(ctx=Depends(run)):
        s, p = ctx
        return view(checkpoint(s, p.tenant_id))

    @app.post("/api/v1/decision/forecasts", status_code=202)
    def forecast(body: ForecastBody, ctx=Depends(run)):
        s, p = ctx
        if any(h < 1 or h > 365 for h in body.horizons):
            raise InvalidInput("forecast horizons must be in [1,365]")
        return view(
            jobs.request(
                s,
                p,
                body.idempotency_key,
                body.horizons,
                body.models,
                body.policy.model_dump() if body.policy else current_policy(s),
                body.checkpoint_id,
                body.calibration_id,
            )
        )

    @app.get("/api/v1/decision/forecasts/{ident}")
    def get_job(ident: uuid.UUID, ctx=Depends(read)):
        s, _ = ctx
        row = s.get(ForecastJob, ident)
        if row is None:
            raise NotFound("forecast job not found")
        return view(row)

    @app.post("/api/v1/decision/forecasts/{ident}/cancel")
    def cancel(ident: uuid.UUID, ctx=Depends(run)):
        s, p = ctx
        row = s.get(ForecastJob, ident, with_for_update=True)
        if row is None:
            raise NotFound("forecast job not found")
        if row.status not in ("queued", "running"):
            raise Conflict("job already finished")
        row.status = "cancelled"
        row.lease_owner = None
        row.lease_expires_at = None
        record_audit(
            s,
            p,
            "forecast.cancel",
            "forecast_job",
            ident,
            after={"status": "cancelled"},
        )
        return view(row)

    @app.post("/api/v1/decision/results/{ident}/evaluate", status_code=201)
    def evaluate(ident: uuid.UUID, body: EvaluateBody, ctx=Depends(run)):
        s, p = ctx
        return view(evaluation.evaluate(s, p, ident, body.truth_checkpoint_id))

    @app.post("/api/v1/decision/calibrations", status_code=201)
    def calibrate(body: CalibrationBody, ctx=Depends(run)):
        s, p = ctx
        return view(evaluation.calibrate(s, p, body.reference_id, body.model))

    @app.post("/api/v1/decision/releases", status_code=201)
    def release(body: ReleaseBody, ctx=Depends(run)):
        s, p = ctx
        cal = verified(s, body.calibration_id, "calibration")
        champion = evaluation.champion_for(s, cal.payload["scope"])
        return view(evaluation.propose_release(s, p, body.calibration_id, champion))

    @app.post("/api/v1/decision/releases/{ident}/{action}")
    def decide_release(
        ident: uuid.UUID,
        action: Literal["activate", "rollback"],
        body: ActionBody,
        ctx=Depends(allowed(sec.FORECAST_APPROVE)),
    ):
        s, p = ctx
        return view(
            evaluation.decide_release(
                s, p, ident, body.expected_revision, action, body.note
            )
        )

    @app.post("/api/v1/decision/boms", status_code=201)
    def draft_bom(body: BomBody, ctx=Depends(allowed(sec.BOM_EDIT))):
        s, p = ctx
        return view(
            bundles.draft(
                s,
                p,
                body.parent,
                [r.model_dump() for r in body.components],
                body.effective_from,
                body.stock_mode,
                body.note,
            )
        )

    @app.post("/api/v1/decision/boms/{ident}/publish")
    def publish_bom(
        ident: uuid.UUID, body: ActionBody, ctx=Depends(allowed(sec.BOM_APPROVE))
    ):
        s, p = ctx
        return view(bundles.publish(s, p, ident, body.expected_revision, body.note))

    @app.get("/api/v1/decision/boms/trace")
    def trace(
        checkpoint_id: uuid.UUID,
        sku: str = Query(min_length=1, max_length=80),
        ctx=Depends(read),
    ):
        s, p = ctx
        return bundles.trace(s, p, checkpoint_id, sku)

    @app.post("/api/v1/decision/boms/capacity")
    def capacity(body: TraceBody, ctx=Depends(read)):
        s, p = ctx
        inp = verified(s, body.input_id, "input")
        if inp.payload.get("schema") != "pit-network-v1":
            raise InvalidInput("select a PIT network input")
        rows = [
            r
            for r in inp.payload["network"]["inventory"]
            if r["warehouse_id"] == body.warehouse_id
        ]
        stock = {r["sku"]: r["on_hand"] - r["allocated"] - r["backorder"] for r in rows}
        if not rows:
            raise InvalidInput("warehouse absent from frozen input")
        return bundles.trace(s, p, inp.checkpoint_id, body.sku, stock, body.allocations)

    @app.post("/api/v1/decision/overrides", status_code=201)
    def draft_override(body: OverrideBody, ctx=Depends(allowed(sec.OVERRIDE_EDIT))):
        s, p = ctx
        return view(
            collaboration.draft(
                s,
                p,
                body.baseline_id,
                body.model,
                body.kind,
                body.start,
                body.end,
                body.total,
                [t.model_dump() for t in body.targets],
                body.expires_at,
                body.note,
            )
        )

    @app.post("/api/v1/decision/overrides/{ident}/{action}")
    def decide_override(
        ident: uuid.UUID,
        action: Literal["approve", "reject", "revoke"],
        body: ActionBody,
        ctx=Depends(allowed(sec.OVERRIDE_APPROVE)),
    ):
        s, p = ctx
        return view(
            collaboration.decide(s, p, ident, body.expected_revision, action, body.note)
        )

    @app.post("/api/v1/decision/scenarios", status_code=201)
    def scenario(body: ScenarioBody, ctx=Depends(allowed(sec.OVERRIDE_EDIT))):
        s, p = ctx
        return view(collaboration.scenario(s, p, body.baseline_id, body.override_ids))

    @app.post("/api/v1/decision/scenarios/{ident}/fva", status_code=201)
    def fva(ident: uuid.UUID, body: FvaBody, ctx=Depends(run)):
        s, p = ctx
        return view(collaboration.fva(s, p, ident, body.evaluation_id))

    @app.post("/api/v1/decision/risks/refresh")
    def refresh_risks(body: RiskBody, ctx=Depends(allowed(sec.RISK_MANAGE))):
        s, p = ctx
        return [view(r) for r in risks.refresh(s, p, body.result_id, current_policy(s))]

    @app.post("/api/v1/decision/risks/{ident}/{action}")
    def disposition(
        ident: uuid.UUID,
        action: Literal["acknowledge", "investigate", "resolve", "suppress"],
        body: ActionBody,
        ctx=Depends(allowed(sec.RISK_MANAGE)),
    ):
        s, p = ctx
        return view(
            risks.disposition(
                s,
                p,
                ident,
                body.expected_revision,
                action,
                body.note,
                body.assignee_id,
                body.suppress_until,
            )
        )
