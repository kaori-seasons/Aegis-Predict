"""Separate organizer and participant projections. No generic case serialization for experts."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..core import security as sec
from ..core.errors import NotFound
from ..core.util import json_safe
from ..decision import experts
from ..decision.history import verified
from ..models import AppUser, DecisionCase, ExpertResponse


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CreateBody(Body):
    idempotency_key: str = Field(min_length=1, max_length=100)
    baseline_id: uuid.UUID
    model: Literal[
        "seasonal-tsb-v2",
        "rolling-champion-v3",
        "market-aware-v4",
        "commerce-coldstart-v5",
    ]
    sku: str = Field(min_length=1, max_length=80)
    warehouse_id: str = Field(min_length=1, max_length=64)
    launch_date: date
    participant_ids: list[uuid.UUID] = Field(min_length=3, max_length=20)


class ResponseBody(Body):
    round_number: int = Field(ge=1, le=5, strict=True)
    low: int = Field(ge=0, le=10000000, strict=True)
    central: int = Field(ge=0, le=10000000, strict=True)
    high: int = Field(ge=0, le=10000000, strict=True)
    additional_supply_units: int = Field(ge=0, le=10000000, strict=True)
    rationale: str = Field(min_length=1, max_length=2000)
    reason_codes: list[
        Literal[
            "analogue_history",
            "stockout",
            "positioning",
            "consumer_price",
            "planned_products",
            "own_experience",
            "missing_data",
        ]
    ] = Field(min_length=1, max_length=7)
    missing_information: str = Field(default="", max_length=1000)
    revision_reason: str = Field(default="", max_length=1000)


class ActionBody(Body):
    expected_revision: int = Field(ge=1, strict=True)
    note: str = Field(min_length=1, max_length=2000)
    refresh_evidence: bool = False
    acknowledge_disagreement: bool = False


class ConvertBody(Body):
    expected_revision: int = Field(ge=1, strict=True)
    baseline_id: uuid.UUID
    expires_at: AwareDatetime
    note: str = Field(min_length=1, max_length=2000)


class Offer(Body):
    minimum_order: Decimal = Field(gt=0, le=1000000000, max_digits=18, decimal_places=6)
    quantity: Decimal = Field(gt=0, le=1000000000, max_digits=18, decimal_places=6)
    unit_price: Decimal = Field(gt=0, le=1000000, max_digits=18, decimal_places=6)
    setup_cost: Decimal = Field(
        default=Decimal(0), ge=0, le=1000000000, max_digits=18, decimal_places=6
    )


class SupplyBody(Body):
    currency: str = Field(pattern="^[A-Z]{3}$")
    material_unit: str = Field(min_length=1, max_length=30)
    material_per_finished_unit: Decimal = Field(
        gt=0, le=1000000, max_digits=18, decimal_places=6
    )
    yield_fraction: Decimal = Field(gt=0, le=1, max_digits=12, decimal_places=6)
    confirmed_finished_units: int = Field(default=0, ge=0, le=10000000, strict=True)
    capital_budget: Decimal | None = Field(
        default=None, gt=0, le=1000000000000, max_digits=18, decimal_places=2
    )
    offers: list[Offer] = Field(min_length=1, max_length=12)


def projection(session, p, r, *, organizer):
    data = r.payload
    current = data["rounds"][-1]
    materials = verified(session, uuid.UUID(current["packet_id"]), "input")
    base = {
        "id": str(r.id),
        "revision": r.revision,
        "status": r.status,
        "round": current["number"],
        "start": data["start"],
        "end": data["end"],
        "sku": data["sku"],
        "warehouse_id": data["warehouse_id"],
        "expires_at": data["expires_at"],
        "packet": materials.payload,
        "packet_digest": materials.digest,
        "participant_count": len(data["participants"]),
        "submitted_count": len(experts.round_rows(session, r)),
        "feedback": [
            verified(session, uuid.UUID(x["feedback_id"]), "input").payload
            for x in data["rounds"]
            if x["feedback_id"]
        ],
        "privacy": "Independent dedicated accounts; organizer cannot inspect sealed individual responses; identities omitted from feedback. Avoid identifying yourself in rationale.",
    }
    if organizer:
        completed = {row.user_id for row in experts.round_rows(session, r)}
        members = list(
            session.scalars(
                select(AppUser).where(
                    AppUser.id.in_([uuid.UUID(x) for x in data["participants"]])
                )
            )
        )
        base["completion"] = [
            {
                "id": str(u.id),
                "label": u.display_name or u.email,
                "submitted": u.id in completed,
            }
            for u in members
        ]
        base["baseline_id"] = data["baseline_id"]
        base["model"] = data["model"]
        base["result_id"] = data.get("result_id")
        base["override_id"] = data.get("override_id")
        if data.get("result_id"):
            base["result"] = verified(
                session, uuid.UUID(data["result_id"]), "input"
            ).payload
    else:
        own = session.scalar(
            select(ExpertResponse).where(
                ExpertResponse.study_id == r.id,
                ExpertResponse.user_id == p.user_id,
                ExpertResponse.round_number == current["number"],
            )
        )
        previous = session.scalar(
            select(ExpertResponse).where(
                ExpertResponse.study_id == r.id,
                ExpertResponse.user_id == p.user_id,
                ExpertResponse.round_number == current["number"] - 1,
            )
        )
        base["own_response"] = own.payload if own else None
        base["previous_response"] = previous.payload if previous else None
    return json_safe(base)


def install(app, allowed, view):
    manage = allowed(sec.EXPERT_MANAGE)
    respond = allowed(sec.EXPERT_RESPOND)
    manage_ctx = Depends(manage)
    respond_ctx = Depends(respond)

    @app.get("/api/v1/expert-studies/participants")
    def participants(ctx=manage_ctx):
        s, _ = ctx
        return [
            {"id": str(r.id), "label": r.display_name or r.email}
            for r in s.scalars(
                select(AppUser)
                .where(AppUser.is_active)
                .order_by(AppUser.email)
                .limit(500)
            )
            if set(r.roles) == {"FORECAST_EXPERT"} and r.location_scope is None
        ]

    @app.get("/api/v1/expert-studies")
    def listed(ctx=manage_ctx):
        s, _ = ctx
        return [
            {
                "id": str(r.id),
                "revision": r.revision,
                "status": r.status,
                "sku": r.payload["sku"],
                "warehouse_id": r.payload["warehouse_id"],
                "round": len(r.payload["rounds"]),
                "created_at": r.created_at,
            }
            for r in s.scalars(
                select(DecisionCase)
                .where(DecisionCase.kind == "expert_study")
                .order_by(DecisionCase.created_at.desc())
                .limit(100)
            )
        ]

    @app.post("/api/v1/expert-studies", status_code=201)
    def create(body: CreateBody, ctx=manage_ctx):
        s, p = ctx
        r = experts.create(
            s,
            p,
            body.idempotency_key,
            body.baseline_id,
            body.model,
            body.sku,
            body.warehouse_id,
            body.launch_date,
            body.participant_ids,
        )
        return projection(s, p, r, organizer=True)

    @app.get("/api/v1/expert-studies/{ident}")
    def get(ident: uuid.UUID, ctx=manage_ctx):
        s, p = ctx
        return projection(s, p, experts.study(s, ident), organizer=True)

    @app.post("/api/v1/expert-studies/{ident}/actions/{action}")
    def action(
        ident: uuid.UUID,
        action: Literal["close", "next", "finalize", "withdraw"],
        body: ActionBody,
        ctx=manage_ctx,
    ):
        s, p = ctx
        r = experts.advance(
            s,
            p,
            ident,
            body.expected_revision,
            action,
            body.note,
            body.refresh_evidence,
            body.acknowledge_disagreement,
        )
        return projection(s, p, r, organizer=True)

    @app.post("/api/v1/expert-studies/{ident}/draft-override", status_code=201)
    def convert(ident: uuid.UUID, body: ConvertBody, ctx=manage_ctx):
        s, p = ctx
        return view(
            experts.convert(
                s,
                p,
                ident,
                body.expected_revision,
                body.baseline_id,
                body.expires_at,
                body.note,
            )
        )

    @app.post("/api/v1/expert-studies/{ident}/supply-comparison", status_code=201)
    def supply(ident: uuid.UUID, body: SupplyBody, ctx=manage_ctx):
        s, p = ctx
        r = experts.compare_supply(s, p, ident, body)
        return {"id": str(r.id), "digest": r.digest, "payload": r.payload}

    @app.get("/api/v1/expert-inbox")
    def inbox(ctx=respond_ctx):
        s, p = ctx
        rows = s.scalars(
            select(DecisionCase)
            .where(
                DecisionCase.kind == "expert_study",
                DecisionCase.payload["participants"].contains([str(p.user_id)]),
            )
            .order_by(DecisionCase.created_at.desc())
            .limit(100)
        )
        return [
            {
                "id": str(r.id),
                "sku": r.payload["sku"],
                "warehouse_id": r.payload["warehouse_id"],
                "status": r.status,
                "round": len(r.payload["rounds"]),
            }
            for r in rows
        ]

    @app.get("/api/v1/expert-inbox/{ident}")
    def question(ident: uuid.UUID, ctx=respond_ctx):
        s, p = ctx
        r = experts.study(s, ident)
        if str(p.user_id) not in r.payload["participants"]:
            raise NotFound("expert study not found")
        return projection(s, p, r, organizer=False)

    @app.post("/api/v1/expert-inbox/{ident}/responses", status_code=201)
    def seal(ident: uuid.UUID, body: ResponseBody, ctx=respond_ctx):
        s, p = ctx
        experts.submit(
            s, p, ident, body.round_number, body.model_dump(exclude={"round_number"})
        )
        return {"status": "sealed", "round": body.round_number}
