"""Approved operational assumptions, exact pack allocations and frozen scenarios."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import json_safe, utcnow
from ..models import DecisionArtifact, DecisionCase
from .bundles import case_for_update, lock, separate_approver
from .forecasting import metrics
from .history import artifact, verified


def allocate_total(total, items, *, whole_packs=False):
    if (
        type(total) is not int
        or not 0 <= total <= 10000000
        or not items
        or len(items) > 100
    ):
        raise InvalidInput("invalid allocation size")
    if any(i["weight"] < 0 for i in items) or not sum(i["weight"] for i in items):
        raise InvalidInput(
            "positive allocation weights required; zero-history groups require an explicit allocation basis"
        )
    if len({(i["sku"], i["warehouse_id"]) for i in items}) != len(items):
        raise InvalidInput("duplicate allocation target")
    weights = [i["weight"] / sum(j["weight"] for j in items) for i in items]
    if not whole_packs:
        targets = [total * w for w in weights]
        units = [int(x) for x in targets]
        for n in sorted(
            range(len(items)),
            key=lambda i: (
                -(targets[i] - units[i]),
                items[i]["sku"],
                items[i]["warehouse_id"],
            ),
        )[: total - sum(units)]:
            units[n] += 1
    else:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp

        n = len(items)
        packs = [i["pack_size"] for i in items]
        rows = []
        lo = []
        hi = []
        rows.append([*packs, *([0] * n)])
        lo.append(total)
        hi.append(total)
        for i, w in enumerate(weights):
            row = [0] * (2 * n)
            row[i] = packs[i]
            row[n + i] = -1
            rows.append(row)
            lo.append(float("-inf"))
            hi.append(total * w)
            row = [0] * (2 * n)
            row[i] = -packs[i]
            row[n + i] = -1
            rows.append(row)
            lo.append(float("-inf"))
            hi.append(-total * w)
        res = milp(
            np.array([1e-8 * (i + 1) for i in range(n)] + [1.0] * n),
            integrality=np.array([1] * n + [0] * n),
            bounds=Bounds(
                np.zeros(2 * n),
                np.array([total // p for p in packs] + [float("inf")] * n),
            ),
            constraints=LinearConstraint(np.array(rows), lo, hi),
            options={"time_limit": 5, "mip_rel_gap": 0},
        )
        if res.x is None:
            raise InvalidInput(
                "purchase intent total cannot be conserved in whole packs"
            )
        units = [int(round(res.x[i])) * packs[i] for i in range(n)]
        if sum(units) != total:
            raise InvalidInput("pack allocation failed exact-total validation")
    return [
        {"sku": r["sku"], "warehouse_id": r["warehouse_id"], "units": v}
        for r, v in zip(items, units)
    ]


def draft(
    session, p, baseline_id, model, kind, start, end, total, items, expires_at, note
):
    lock(session, p.tenant_id)
    baseline = verified(session, baseline_id, "forecast")
    inp = verified(session, uuid.UUID(baseline.payload["input_id"]), "input")
    if kind not in ("demand_forecast", "purchase_intent") or not note.strip():
        raise InvalidInput("assumption type and reason required")
    if (
        expires_at.tzinfo is None
        or expires_at <= utcnow()
        or end < start
        or (end - start).days > 364
    ):
        raise InvalidInput("invalid assumption dates or expiry")
    valid = {
        (r["sku"], r["warehouse_id"]): r
        for r in baseline.payload["rows"]
        if r["model"] == model
    }
    packs = {s["sku"]: s["pack_size"] for s in inp.payload["network"]["skus"]}
    enriched = []
    for item in items:
        row = valid.get((item["sku"], item["warehouse_id"]))
        if row is None:
            raise InvalidInput("allocation target is absent from frozen baseline model")
        tz = ZoneInfo(row["timezone"])
        today = utcnow().astimezone(tz).date()
        first = date.fromisoformat(next(iter(row["horizons"].values()))["target_start"])
        if (
            start <= today
            or start < first
            or end >= first + timedelta(days=len(row["daily"]))
        ):
            raise InvalidInput(
                "assumptions must cover strictly future days within the frozen baseline trajectory"
            )
        enriched.append(dict(item, pack_size=packs[item["sku"]]))
    allocations = allocate_total(total, enriched, whole_packs=kind == "purchase_intent")
    row = DecisionCase(
        tenant_id=p.tenant_id,
        kind="override",
        scope_key=uuid.uuid4().hex,
        status="draft",
        created_by=p.user_id,
        payload=json_safe(
            {
                "baseline_id": str(baseline.id),
                "model": model,
                "kind": kind,
                "start": start,
                "end": end,
                "total": total,
                "allocations": allocations,
                "weights": items,
                "expires_at": expires_at,
                "note": note,
            }
        ),
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        p,
        "forecast.override.draft",
        "decision_case",
        row.id,
        after=row.payload,
    )
    return row


def decide(session, p, ident, revision, action, note):
    lock(session, p.tenant_id)
    row = case_for_update(session, ident, "override", revision)
    if not note.strip():
        raise InvalidInput("decision reason required")
    if action in ("approve", "reject"):
        if row.status != "draft":
            raise Conflict("only drafts can be approved or rejected")
        separate_approver(row, p)
        if datetime.fromisoformat(row.payload["expires_at"]) <= utcnow():
            raise Conflict("assumption expired")
        if action == "approve":
            for other in session.scalars(
                select(DecisionCase).where(
                    DecisionCase.kind == "override", DecisionCase.status == "approved"
                )
            ):
                a, b = row.payload, other.payload
                if (
                    a["kind"] == b["kind"]
                    and a["model"] == b["model"]
                    and a["baseline_id"] == b["baseline_id"]
                    and a["start"] <= b["end"]
                    and b["start"] <= a["end"]
                    and datetime.fromisoformat(b["expires_at"]) > utcnow()
                ):
                    if {(r["sku"], r["warehouse_id"]) for r in a["allocations"]} & {
                        (r["sku"], r["warehouse_id"]) for r in b["allocations"]
                    }:
                        raise Conflict(
                            "approved assumptions overlap for the same target and type"
                        )
        row.status = "approved" if action == "approve" else "rejected"
        row.approved_by = p.user_id
    elif action == "revoke":
        if row.status != "approved":
            raise Conflict("only approved assumptions can be revoked")
        row.status = "revoked"
    else:
        raise InvalidInput("unknown assumption action")
    row.revision += 1
    record_audit(
        session,
        p,
        "forecast.override." + action,
        "decision_case",
        row.id,
        after={"status": row.status, "note": note},
    )
    return row


def scenario(session, p, baseline_id, override_ids):
    lock(session, p.tenant_id)
    baseline = verified(session, baseline_id, "forecast")
    if (
        not override_ids
        or len(set(override_ids)) != len(override_ids)
        or len(override_ids) > 100
    ):
        raise InvalidInput("choose1–100 distinct approved assumptions")
    overrides = []
    for ident in override_ids:
        row = session.get(DecisionCase, ident, with_for_update=True)
        if row is None or row.kind != "override" or row.status != "approved":
            raise InvalidInput("approved assumption unavailable")
        data = row.payload
        if (
            data["baseline_id"] != str(baseline.id)
            or datetime.fromisoformat(data["expires_at"]) <= utcnow()
        ):
            raise InvalidInput("assumption baseline differs or has expired")
        for target in data["allocations"]:
            fc = next(
                r
                for r in baseline.payload["rows"]
                if (r["sku"], r["warehouse_id"], r["model"])
                == (target["sku"], target["warehouse_id"], data["model"])
            )
            if (
                date.fromisoformat(data["start"])
                <= utcnow().astimezone(ZoneInfo(fc["timezone"])).date()
            ):
                raise InvalidInput("cannot create a retrospective operational scenario")
        overrides.append({"id": str(row.id), "revision": row.revision, **data})
    payload = json_safe(baseline.payload)
    payload["baseline_id"] = str(baseline.id)
    payload["overrides"] = overrides
    covered = set()
    purchase = []
    for ov in overrides:
        for target in ov["allocations"]:
            row = next(
                r
                for r in payload["rows"]
                if (r["sku"], r["warehouse_id"], r["model"])
                == (target["sku"], target["warehouse_id"], ov["model"])
            )
            if ov["kind"] == "purchase_intent":
                purchase.append(
                    {
                        **target,
                        "start": ov["start"],
                        "end": ov["end"],
                        "override_id": ov["id"],
                        "model": ov["model"],
                    }
                )
                continue
            first = date.fromisoformat(
                next(iter(row["horizons"].values()))["target_start"]
            )
            lo = (date.fromisoformat(ov["start"]) - first).days
            hi = (date.fromisoformat(ov["end"]) - first).days + 1
            for d in range(lo, hi):
                key = (row["sku"], row["warehouse_id"], row["model"], d)
                if key in covered:
                    raise Conflict("overlapping demand assumptions in scenario")
                covered.add(key)
            subtotal = sum(row["daily"][lo:hi])
            length = hi - lo
            new = (
                [target["units"] * x / subtotal for x in row["daily"][lo:hi]]
                if subtotal
                else [target["units"] / length] * length
            )
            row["daily"][lo:hi] = new
            for h, interval in row["horizons"].items():
                delta = sum(row["daily"][: int(h)]) - interval["expected"]
                interval.update(
                    expected=interval["expected"] + delta,
                    p50=max(0, interval["p50"] + delta),
                    p80=max(0, interval["p80"] + delta),
                    p95=max(0, interval["p95"] + delta),
                    calibrated=False,
                )
            row["operational_adjustment"] = True
    from .forecasting import monotone_intervals

    for row in payload["rows"]:
        monotone_intervals(row["horizons"])
    payload["purchase_intents"] = purchase
    payload["created_at"] = utcnow()
    payload["uncertainty"] = (
        "human shifts are uncalibrated; preserve empirical baseline separately"
    )
    result = artifact(
        session,
        p.tenant_id,
        "scenario",
        uuid.uuid4().hex,
        payload,
        parent_id=baseline.id,
        actor=p.user_id,
    )
    record_audit(
        session,
        p,
        "forecast.scenario.create",
        "decision_artifact",
        result.id,
        after={"baseline_id": baseline.id, "override_ids": override_ids},
    )
    return result


def fva(session, p, scenario_id, evaluation_id):
    sc = verified(session, scenario_id, "scenario")
    ev = verified(session, evaluation_id, "evaluation")
    if ev.payload["forecast_id"] != sc.payload["baseline_id"]:
        raise InvalidInput(
            "FVA requires the identical frozen baseline and truth revision"
        )
    # Compare full identical issued windows only. Purchase intent never changes a
    # demand forecast; it is excluded from forecast value-added calculations.
    bykey = {(r["sku"], r["warehouse_id"], r["model"]): r for r in sc.payload["rows"]}
    base = []
    adjusted = []
    for r in ev.payload["rows"]:
        row = bykey[(r["sku"], r["warehouse_id"], r["model"])]
        if not row.get("operational_adjustment"):
            continue
        interval = row["horizons"][str(r["horizon"])]
        base.append(r)
        adjusted.append(
            r
            | {k: interval[k] for k in ("expected", "p50", "p80", "p95", "calibrated")}
        )
    b, a = metrics(base), metrics(adjusted)
    existing = session.scalar(
        select(DecisionArtifact).where(
            DecisionArtifact.kind == "evaluation",
            DecisionArtifact.identity == f"fva:{sc.id}:{ev.id}",
        )
    )
    if existing:
        return existing
    result = artifact(
        session,
        p.tenant_id,
        "evaluation",
        f"fva:{sc.id}:{ev.id}",
        {
            "schema": "operational-fva-v1",
            "scenario_id": str(sc.id),
            "baseline_evaluation_id": str(ev.id),
            "scope": sc.payload["scope"],
            "baseline": b,
            "operational": a,
            "mae_value_added": b["mae"] - a["mae"] if base else None,
            "status": "mature" if base else "waiting_for_identical_mature_actuals",
            "rows": [],
            "excluded_purchase_intents": len(sc.payload["purchase_intents"]),
        },
        parent_id=sc.id,
        actor=p.user_id,
    )
    return result
