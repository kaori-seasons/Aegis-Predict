"""Independent sealed rounds, anonymous feedback, separately approved operational use."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from statistics import median, quantiles
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from ..core.util import content_hash, json_safe, utcnow
from ..models import AppUser, DecisionCase, ExpertResponse
from ..planning.policy import PlanningPolicy
from .analogues import evidence
from .bundles import case_for_update, lock
from .history import artifact, checkpoint, freeze_input, verified

REASONS = {
    "analogue_history",
    "stockout",
    "positioning",
    "consumer_price",
    "planned_products",
    "own_experience",
    "missing_data",
}
MAX_ROUNDS = 5


def study(session, ident, *, write=False):
    r = session.get(DecisionCase, ident, with_for_update=write)
    if r is None or r.kind != "expert_study":
        raise NotFound("expert study not found")
    return r


def ensure_future(r):
    if datetime.fromisoformat(r.payload["expires_at"]) <= utcnow():
        raise Conflict(
            "study expired; refresh evidence and start a new pre-launch study"
        )


def round_rows(session, r):
    return list(
        session.scalars(
            select(ExpertResponse)
            .where(
                ExpertResponse.study_id == r.id,
                ExpertResponse.round_number == r.payload["rounds"][-1]["number"],
            )
            .order_by(ExpertResponse.created_at)
        )
    )


def packet(session, input_id, sku, warehouse, start, p):
    inp = verified(session, input_id, "input")
    payload = evidence(inp.payload, sku, warehouse, start)
    # Deliberate allow-list: no supplier terms, MOQ, costs, baseline quantities or purchase recommendations.
    return artifact(
        session,
        p.tenant_id,
        "input",
        uuid.uuid4().hex,
        payload,
        checkpoint_id=inp.checkpoint_id,
        actor=p.user_id,
    )


def create(session, p, key, baseline_id, model, sku, warehouse, start, participant_ids):
    lock(session, p.tenant_id)
    signature = content_hash(
        [
            baseline_id,
            model,
            sku,
            warehouse,
            start,
            sorted(str(i) for i in participant_ids),
        ]
    )
    existing = session.scalar(
        select(DecisionCase).where(
            DecisionCase.kind == "expert_study",
            DecisionCase.scope_key == "expert:" + key,
        )
    )
    if existing:
        if existing.payload["request_hash"] != signature:
            raise Conflict("study key already used with another request")
        return existing
    if p.user_id is None:
        raise InvalidInput("a named organizer is required")
    if (
        not 3 <= len(participant_ids) <= 20
        or len(set(participant_ids)) != len(participant_ids)
        or p.user_id in participant_ids
    ):
        raise InvalidInput(
            "select3–20 distinct independent expert accounts, excluding the organizer"
        )
    members = list(
        session.scalars(
            select(AppUser).where(AppUser.id.in_(participant_ids), AppUser.is_active)
        )
    )
    if len(members) != len(participant_ids) or any(
        set(u.roles) != {"FORECAST_EXPERT"} or u.location_scope is not None
        for u in members
    ):
        raise InvalidInput(
            "participants must be active, same-tenant dedicated FORECAST_EXPERT accounts with global scope"
        )
    fc = verified(session, baseline_id, "forecast")
    inp = verified(session, uuid.UUID(fc.payload["input_id"]), "input")
    row = next(
        (
            r
            for r in fc.payload["rows"]
            if (r["sku"], r["warehouse_id"], r["model"]) == (sku, warehouse, model)
        ),
        None,
    )
    if not row:
        raise InvalidInput("target model absent from frozen baseline")
    first = date.fromisoformat(next(iter(row["horizons"].values()))["target_start"])
    end = start + timedelta(days=29)
    if start < first or end >= first + timedelta(days=len(row["daily"])):
        raise InvalidInput(
            "baseline trajectory must cover the entire30day launch window"
        )
    materials = packet(session, inp.id, sku, warehouse, start, p)
    expiry = datetime.combine(start, datetime.min.time(), ZoneInfo(row["timezone"]))
    if expiry <= utcnow() or (expiry - utcnow()).days > 90:
        raise InvalidInput("planned launch must be within90days and strictly future")
    result = DecisionCase(
        tenant_id=p.tenant_id,
        kind="expert_study",
        scope_key="expert:" + key,
        status="open",
        created_by=p.user_id,
        artifact_id=materials.id,
        payload=json_safe(
            {
                "request_hash": signature,
                "baseline_id": baseline_id,
                "model": model,
                "sku": sku,
                "warehouse_id": warehouse,
                "start": start,
                "end": end,
                "timezone": row["timezone"],
                "expires_at": expiry,
                "participants": [str(i) for i in participant_ids],
                "rounds": [
                    {"number": 1, "packet_id": str(materials.id), "feedback_id": None}
                ],
                "input_id": str(inp.id),
                "scope": fc.payload["scope"],
                "max_rounds": MAX_ROUNDS,
            }
        ),
    )
    session.add(result)
    session.flush()
    record_audit(
        session,
        p,
        "expert.study.create",
        "decision_case",
        result.id,
        after={"participants": len(participant_ids), "packet_digest": materials.digest},
    )
    return result


def feedback(rows, previous=None):
    if len(rows) < 3:
        raise InvalidInput("at least3judgments required")
    central = [r.payload["central"] for r in rows]
    q = quantiles(central, n=4, method="inclusive")
    mid = median(central)
    relative_iqr = (q[2] - q[0]) / mid if mid else (0 if max(central) == 0 else None)
    change = (
        abs(mid - previous["central_median"]) / previous["central_median"]
        if previous and previous["central_median"]
        else (0 if previous and mid == 0 else None)
    )
    return {
        "schema": "expert-anonymous-feedback-v1",
        "count": len(rows),
        "central_median": mid,
        "central_min": min(central),
        "central_max": max(central),
        "central_p25": q[0],
        "central_p75": q[2],
        "low_median": median(r.payload["low"] for r in rows),
        "high_median": median(r.payload["high"] for r in rows),
        "relative_iqr": relative_iqr,
        "median_relative_change": change,
        "stable": bool(
            previous
            and relative_iqr is not None
            and relative_iqr <= 0.5
            and change is not None
            and change <= 0.1
        ),
        "anonymous_judgments": sorted(
            [
                {
                    "low": r.payload["low"],
                    "central": r.payload["central"],
                    "high": r.payload["high"],
                    "rationale": r.payload["rationale"],
                    "reason_codes": r.payload["reason_codes"],
                    "missing_information": r.payload["missing_information"],
                }
                for r in rows
            ],
            key=lambda x: (x["central"], x["rationale"]),
        ),
        "interpretation": "equal-weight descriptive judgment statistics; dispersion is not a calibrated prediction interval or evidence of accuracy",
        "stability_rule": {
            "minimum_closed_rounds": 2,
            "max_relative_iqr": 0.5,
            "max_median_relative_change": 0.1,
        },
    }


def submit(session, p, ident, round_number, payload):
    r = study(session, ident, write=True)
    if str(p.user_id) not in r.payload["participants"]:
        raise NotFound("expert study not found")
    ensure_future(r)
    current = r.payload["rounds"][-1]
    if r.status != "open" or current["number"] != round_number:
        raise Conflict("round no longer open; reload the question packet")
    if any(
        type(payload.get(k)) is not int or not 0 <= payload[k] <= 10000000
        for k in ("low", "central", "high", "additional_supply_units")
    ):
        raise InvalidInput(
            "judgments and separate supply intent must be bounded whole units"
        )
    if (
        not (0 <= payload["low"] <= payload["central"] <= payload["high"] <= 10000000)
        or not payload["rationale"].strip()
        or not set(payload["reason_codes"]) <= REASONS
        or not payload["reason_codes"]
    ):
        raise InvalidInput(
            "ordered demand judgments, rationale and supported reason codes required"
        )
    user = session.get(AppUser, p.user_id)
    if not user or not user.is_active or set(user.roles) != {"FORECAST_EXPERT"}:
        raise PermissionDenied("dedicated active expert role required")
    previous = session.scalar(
        select(ExpertResponse).where(
            ExpertResponse.study_id == r.id,
            ExpertResponse.user_id == p.user_id,
            ExpertResponse.round_number == round_number - 1,
        )
    )
    if (
        previous
        and previous.payload["central"] != payload["central"]
        and not payload["revision_reason"].strip()
    ):
        raise InvalidInput("explain the change from the prior round")
    digest = content_hash(payload)
    old = session.scalar(
        select(ExpertResponse).where(
            ExpertResponse.study_id == r.id,
            ExpertResponse.round_number == round_number,
            ExpertResponse.user_id == p.user_id,
        )
    )
    if old:
        if old.digest != digest:
            raise Conflict(
                "sealed judgment cannot be overwritten; revise in the next round"
            )
        return old
    result = ExpertResponse(
        tenant_id=p.tenant_id,
        study_id=r.id,
        user_id=p.user_id,
        round_number=round_number,
        payload=json_safe(payload),
        digest=digest,
    )
    session.add(result)
    session.flush()
    record_audit(
        session,
        p,
        "expert.response.seal",
        "decision_case",
        r.id,
        after={"round": round_number, "digest": digest},
    )
    return result


def advance(
    session, p, ident, revision, action, note, refresh=False, acknowledge=False
):
    lock(session, p.tenant_id)
    r = case_for_update(session, ident, "expert_study", revision)
    if not note.strip():
        raise InvalidInput("action reason required")
    data = json_safe(r.payload)
    current = data["rounds"][-1]
    if action == "withdraw":
        if r.status == "withdrawn":
            raise Conflict("study already withdrawn")
        if data.get("override_id"):
            ov = session.get(
                DecisionCase, uuid.UUID(data["override_id"]), with_for_update=True
            )
            if ov.status in ("draft", "approved"):
                ov.status = "revoked"
                ov.revision += 1
                record_audit(
                    session,
                    p,
                    "forecast.override.revoke",
                    "decision_case",
                    ov.id,
                    after={
                        "reason": "linked expert study withdrawn",
                        "study_id": str(r.id),
                    },
                )
        r.status = "withdrawn"
    else:
        ensure_future(r)
        if action == "close":
            if r.status != "open":
                raise Conflict("round not open")
            rows = round_rows(session, r)
            if len(rows) != len(data["participants"]):
                raise Conflict(
                    "all frozen participants must submit; do not silently exclude missing judgments"
                )
            previous = (
                verified(
                    session, uuid.UUID(data["rounds"][-2]["feedback_id"]), "input"
                ).payload
                if len(data["rounds"]) > 1
                else None
            )
            report = feedback(rows, previous)
            report.update(
                round_number=current["number"], packet_id=current["packet_id"]
            )
            closed = artifact(
                session, p.tenant_id, "input", uuid.uuid4().hex, report, actor=p.user_id
            )
            current["feedback_id"] = str(closed.id)
            current["closed_at"] = utcnow().isoformat()
            r.status = "round_closed"
        elif action == "next":
            if r.status != "round_closed" or len(data["rounds"]) >= MAX_ROUNDS:
                raise Conflict("close the round first; maximum5rounds")
            materials = verified(session, uuid.UUID(current["packet_id"]), "input")
            if refresh:
                old = verified(session, uuid.UUID(data["input_id"]), "input")
                new = freeze_input(
                    session,
                    p,
                    checkpoint(session, p.tenant_id),
                    PlanningPolicy.model_validate(old.payload["policy"]),
                )
                materials = packet(
                    session,
                    new.id,
                    data["sku"],
                    data["warehouse_id"],
                    date.fromisoformat(data["start"]),
                    p,
                )
            materials = artifact(
                session,
                p.tenant_id,
                "input",
                uuid.uuid4().hex,
                {**materials.payload, "organizer_clarification": note},
                actor=p.user_id,
            )
            data["rounds"].append(
                {
                    "number": len(data["rounds"]) + 1,
                    "packet_id": str(materials.id),
                    "feedback_id": None,
                }
            )
            r.status = "open"
        elif action == "finalize":
            if r.status != "round_closed" or len(data["rounds"]) < 2:
                raise Conflict("at least2complete independent rounds required")
            report = verified(
                session, uuid.UUID(current["feedback_id"]), "input"
            ).payload
            if not report["stable"] and not acknowledge:
                raise Conflict(
                    "disagreement remains; explicitly acknowledge and explain or open another round"
                )
            rows = round_rows(session, r)
            result = artifact(
                session,
                p.tenant_id,
                "input",
                uuid.uuid4().hex,
                {
                    "schema": "expert-consensus-v1",
                    "study_id": str(r.id),
                    "demand": report,
                    "additional_supply_units_median": median(
                        x.payload["additional_supply_units"] for x in rows
                    ),
                    "rounds": data["rounds"],
                    "acknowledged_disagreement": not report["stable"],
                    "note": note,
                    "start": data["start"],
                    "end": data["end"],
                    "use": "human demand judgment only; approval and empirical FVA required; no calibrated probability or automatic model promotion",
                },
                actor=p.user_id,
            )
            data["result_id"] = str(result.id)
            r.status = "ready"
        else:
            raise InvalidInput("unknown study action")
    r.payload = data
    r.revision += 1
    record_audit(
        session,
        p,
        "expert.study." + action,
        "decision_case",
        r.id,
        after={"round": current["number"], "note": note, "status": r.status},
    )
    return r


def convert(session, p, ident, revision, baseline_id, expires_at, note):
    from . import collaboration

    lock(session, p.tenant_id)
    r = study(session, ident, write=True)
    signature = content_hash([baseline_id, expires_at, note])
    if r.payload.get("override_id"):
        if r.status != "converted":
            raise Conflict("expert result withdrawn")
        if r.payload.get("conversion_hash") != signature:
            raise Conflict("expert result already converted with a different request")
        return session.get(DecisionCase, uuid.UUID(r.payload["override_id"]))
    if r.revision != revision or r.status != "ready":
        raise Conflict("ready study and current revision required")
    ensure_future(r)
    fc = verified(session, baseline_id, "forecast")
    if (
        not 0
        <= (utcnow() - datetime.fromisoformat(fc.payload["as_of"])).total_seconds()
        <= 86400
    ):
        raise InvalidInput("use a fresh forecast issued within24hours")
    inp = verified(session, uuid.UUID(fc.payload["input_id"]), "input")
    old = verified(session, uuid.UUID(r.payload["input_id"]), "input")
    if inp.payload["watermarks"]["authorities"] != old.payload["watermarks"][
        "authorities"
    ] or inp.payload["fact_revisions"].get("bom_versions", []) != old.payload[
        "fact_revisions"
    ].get("bom_versions", []):
        raise InvalidInput("demand/BOM contract changed; create a new expert study")
    if expires_at > datetime.fromisoformat(r.payload["expires_at"]):
        raise InvalidInput("assumption must expire no later than launch")
    result = verified(session, uuid.UUID(r.payload["result_id"]), "input")
    total = int(
        Decimal(str(result.payload["demand"]["central_median"])).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )
    ov = collaboration.draft(
        session,
        p,
        baseline_id,
        r.payload["model"],
        "demand_forecast",
        date.fromisoformat(r.payload["start"]),
        date.fromisoformat(r.payload["end"]),
        total,
        [
            {
                "sku": r.payload["sku"],
                "warehouse_id": r.payload["warehouse_id"],
                "weight": 1,
            }
        ],
        expires_at,
        note,
    )
    ov.payload = {
        **ov.payload,
        "expert_study_id": str(r.id),
        "expert_result_id": str(result.id),
        "expert_result_digest": result.digest,
        "expert_original_baseline": r.payload["baseline_id"],
    }
    r.payload = {**r.payload, "override_id": str(ov.id), "conversion_hash": signature}
    r.status = "converted"
    r.revision += 1
    record_audit(
        session,
        p,
        "expert.study.convert",
        "decision_case",
        r.id,
        after={
            "override_id": str(ov.id),
            "total": total,
            "rounding": "median then integer HALF_UP",
        },
    )
    return ov


def compare_supply(session, p, ident, body):
    r = study(session, ident)
    if r.status not in ("ready", "converted"):
        raise Conflict("freeze demand consensus before showing procurement terms")
    result = verified(session, uuid.UUID(r.payload["result_id"]), "input")
    fixed = Decimal(str(result.payload["demand"]["central_median"]))
    consumption = body.material_per_finished_unit
    yield_rate = body.yield_fraction
    rows = []
    for offer in body.offers:
        if offer.quantity < offer.minimum_order:
            raise InvalidInput("purchase quantity is below its MOQ")
        capacity = int(
            (offer.quantity * yield_rate / consumption).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        cost = offer.quantity * offer.unit_price + offer.setup_cost
        if cost > Decimal("1e12"):
            raise InvalidInput("scenario cost exceeds budget limits")
        rows.append(
            {
                "material_quantity": str(offer.quantity),
                "minimum_order": str(offer.minimum_order),
                "unit_price": str(offer.unit_price),
                "setup_cost": str(offer.setup_cost),
                "total_cost": str(cost.quantize(Decimal(".01"))),
                "finished_equivalent_capacity": capacity,
                "shortfall_vs_fixed_demand": str(
                    max(Decimal(0), fixed - body.confirmed_finished_units - capacity)
                ),
                "excess_vs_fixed_demand": str(
                    max(Decimal(0), capacity + body.confirmed_finished_units - fixed)
                ),
                "over_capital_budget": body.capital_budget is not None
                and cost > body.capital_budget,
            }
        )
    return artifact(
        session,
        p.tenant_id,
        "input",
        uuid.uuid4().hex,
        {
            "schema": "procurement-separate-from-demand-v1",
            "study_id": str(r.id),
            "demand_result_id": str(result.id),
            "demand_result_digest": result.digest,
            "fixed_demand_units": str(fixed),
            "currency": body.currency,
            "material_unit": body.material_unit,
            "material_per_finished_unit": str(consumption),
            "yield_fraction": str(yield_rate),
            "confirmed_finished_units": str(body.confirmed_finished_units),
            "offers": rows,
            "interpretation": "read-only all-units quote comparison with explicit conversion/yield; no MRP, purchase execution or change to demand",
        },
        actor=p.user_id,
    )
