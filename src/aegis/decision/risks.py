"""Evidence-deduplicated inventory risks and an audited disposition state machine."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import content_hash, json_safe, utcnow
from ..models import AppUser, DecisionCase
from ..planning.policy import PlanningPolicy
from .bundles import case_for_update, lock
from .history import checkpoint, freeze_input, verified

RULE_VERSION = "inventory-risk-v1"


def observe(session, p, key, condition, evidence, payload, *, immediate=False):
    row = session.scalar(
        select(DecisionCase)
        .where(DecisionCase.kind == "risk", DecisionCase.scope_key == key)
        .with_for_update()
    )
    if row is None:
        row = DecisionCase(
            tenant_id=p.tenant_id,
            kind="risk",
            scope_key=key,
            status="watching",
            created_by=p.user_id,
            payload={
                "rule_version": RULE_VERSION,
                "confirmations": 0,
                "evidence_keys": [],
                "condition": False,
            },
        )
        session.add(row)
        session.flush()
    data = dict(row.payload)
    seen = list(data.get("evidence_keys", []))
    new = evidence not in seen
    if new:
        seen.append(evidence)
        seen = seen[-100:]
        data["confirmations"] = data.get("confirmations", 0) + 1 if condition else 0
    data.update(
        payload,
        condition=condition,
        evidence_keys=seen,
        last_evidence=evidence,
        last_seen_at=utcnow(),
        rule_version=RULE_VERSION,
    )
    suppressed = (
        data.get("suppress_until")
        and datetime.fromisoformat(data["suppress_until"]) > utcnow()
    )
    if condition:
        if (
            (immediate or data["confirmations"] >= 2)
            and not suppressed
            and row.status in ("watching", "resolved", "suppressed")
        ):
            row.status = "open"
    else:
        data["confirmations"] = 0
        if row.status == "watching":
            row.status = "resolved"
    if new or row.payload.get("condition") != condition:
        row.revision += 1
        record_audit(
            session,
            p,
            "risk.observe",
            "decision_case",
            row.id,
            after={
                "evidence": evidence,
                "condition": condition,
                "status": row.status,
                **payload,
            },
        )
    row.payload = json_safe(data)
    return row


def refresh(session, p, result_id, policy):
    lock(session, p.tenant_id)
    fc = verified(session, result_id, "forecast")
    config = PlanningPolicy.model_validate(policy)
    # A risk check must never silently relax caller data-quality policy.
    config = config.model_copy(update={"allow_stale_data": False})
    cp = checkpoint(session, p.tenant_id)
    try:
        with session.begin_nested():
            inp = freeze_input(session, p, cp, config)
    except InvalidInput as exc:
        observe(
            session,
            p,
            "data_unknown",
            True,
            content_hash({"rule": RULE_VERSION, "error": str(exc)}),
            {
                "type": "data_unknown",
                "reason": str(exc),
                "severity": "critical",
                "checkpoint_id": str(cp.id),
            },
            immediate=True,
        )
        return list(
            session.scalars(
                select(DecisionCase)
                .where(DecisionCase.kind == "risk")
                .order_by(DecisionCase.updated_at.desc())
                .limit(200)
            )
        )
    now = utcnow()
    from .forecasting import scope

    mismatch = (
        fc.payload["scope"] != scope(inp.payload, fc.payload["horizons"])
        or config.forecast_model_version not in fc.payload["models"]
    )
    stale = (
        now - datetime.fromisoformat(fc.payload["as_of"])
    ).total_seconds() > 86400 or mismatch
    signature = content_hash(
        {
            "facts": {
                k: v
                for k, v in inp.payload["fact_revisions"].items()
                if k
                in (
                    "inventory_snapshot",
                    "source_authority",
                    "purchase_order_line",
                    "inventory_inbound_link",
                    "inventory_reservation",
                )
            },
            "forecast_id": str(fc.id),
            "rule": RULE_VERSION,
        }
    )
    observe(
        session,
        p,
        "data_unknown",
        stale,
        signature,
        {
            "type": "data_unknown",
            "reason": "forecast older than24h"
            if stale
            else "data coverage and freshness checks pass",
            "severity": "critical",
            "checkpoint_id": str(cp.id),
        },
        immediate=True,
    )
    if stale:
        return list(
            session.scalars(
                select(DecisionCase).where(DecisionCase.kind == "risk").limit(200)
            )
        )
    stock = {
        (r["sku"], r["warehouse_id"]): r for r in inp.payload["network"]["inventory"]
    }
    model = config.forecast_model_version
    for row in fc.payload["rows"]:
        if row["model"] != model:
            continue
        key = (row["sku"], row["warehouse_id"])
        inv = stock.get(key)
        if inv is None:
            continue
        inbound = inp.payload["arrivals"].get(
            "/".join(key), {"arrivals": [], "unknown_inbound_units": 0}
        )
        available = inv["on_hand"] - inv["allocated"] - inv["backorder"]
        first_short = None
        first = date.fromisoformat(next(iter(row["horizons"].values()))["target_start"])
        offset = (
            cp.captured_at.astimezone(ZoneInfo(row["timezone"])).date() - first
        ).days
        if offset < 0:
            continue
        for day, demand in enumerate(row["daily"][offset:], 1):
            available += sum(a["units"] for a in inbound["arrivals"] if a["day"] == day)
            available -= demand
            if available < 0 and first_short is None:
                first_short = day
        detail = {
            "sku": key[0],
            "warehouse_id": key[1],
            "forecast_id": str(fc.id),
            "checkpoint_id": str(cp.id),
            "first_shortage_day": first_short,
            "projected_end_units": available,
            "unknown_inbound_units": inbound["unknown_inbound_units"],
        }
        condition = first_short is not None
        observe(
            session,
            p,
            "stockout:" + ":".join(key),
            condition,
            signature,
            {
                **detail,
                "type": "projected_stockout",
                "severity": "critical"
                if first_short is not None and first_short <= config.review_period_days
                else "warning",
            },
            immediate=inv["on_hand"] - inv["allocated"] - inv["backorder"] < 0,
        )
        demand = sum(row["daily"][offset:])
        slow = available > max(1, demand) * 2
        observe(
            session,
            p,
            "slow:" + ":".join(key),
            slow,
            signature,
            {**detail, "type": "slow_moving_excess", "severity": "warning"},
        )
        unknown = inbound["unknown_inbound_units"] > 0
        observe(
            session,
            p,
            "inbound_unknown:" + ":".join(key),
            unknown,
            signature,
            {**detail, "type": "inbound_eta_unknown", "severity": "critical"},
            immediate=True,
        )
        calibrated = all(r["calibrated"] for r in row["horizons"].values())
        observe(
            session,
            p,
            "calibration:" + ":".join(key),
            not calibrated,
            signature,
            {**detail, "type": "uncalibrated_model", "severity": "warning"},
            immediate=True,
        )
    return list(
        session.scalars(
            select(DecisionCase)
            .where(DecisionCase.kind == "risk")
            .order_by(DecisionCase.updated_at.desc())
            .limit(200)
        )
    )


def disposition(
    session, p, ident, revision, action, note, assignee_id=None, suppress_until=None
):
    row = case_for_update(session, ident, "risk", revision)
    if not note.strip():
        raise InvalidInput("risk disposition reason required")
    if assignee_id:
        user = session.get(AppUser, assignee_id)
        if user is None or not user.is_active:
            raise InvalidInput("active tenant assignee required")
        row.assignee_id = assignee_id
    if action == "acknowledge":
        if row.status not in ("open", "acknowledged", "investigating"):
            raise Conflict("risk is not open")
        row.status = "acknowledged"
    elif action == "investigate":
        if row.status not in ("open", "acknowledged", "investigating"):
            raise Conflict("risk is not open")
        row.status = "investigating"
    elif action == "resolve":
        if row.payload["condition"]:
            raise Conflict(
                "risk condition still exists; refresh evidence after fixing the source or inventory"
            )
        if utcnow() - datetime.fromisoformat(row.payload["last_seen_at"]) > timedelta(
            hours=2
        ):
            raise Conflict("clearance evidence expired; refresh first")
        row.status = "resolved"
    elif action == "suppress":
        if (
            suppress_until is None
            or suppress_until.tzinfo is None
            or not utcnow() < suppress_until <= utcnow() + timedelta(days=7)
        ):
            raise InvalidInput("suppression requires an expiry within7days")
        row.status = "suppressed"
        row.payload = {**row.payload, "suppress_until": suppress_until.isoformat()}
    else:
        raise InvalidInput("unknown risk disposition")
    row.revision += 1
    record_audit(
        session,
        p,
        "risk." + action,
        "decision_case",
        row.id,
        after={
            "status": row.status,
            "note": note,
            "assignee_id": assignee_id,
            "suppress_until": suppress_until,
        },
    )
    return row
