"""Mature, paired evaluation; frozen labels; temporal split conformal calibration.

No endpoint accepts actual values, residuals or promotion metrics. All evidence is
computed from checkpoint-visible authoritative facts and immutable predictions.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import content_hash, utcnow
from ..models import (
    DecisionArtifact,
    DecisionCase,
    ForecastJob,
)
from .bundles import case_for_update, lock, separate_approver
from .forecasting import metrics
from .history import artifact, checkpoint, replay, require_checkpoint, verified

GATES = {
    "minimum_samples": 500,
    "minimum_origins": 8,
    "minimum_series": 30,
    "relative_mae_gain": 0.02,
    "maximum_abs_bias": 0.10,
    "maximum_group_regression": 0.02,
    "coverage_low": 0.92,
    "coverage_high": 0.98,
    "maximum_job_failure_rate": 0.001,
}


def evaluate(session, p, result_id, truth_checkpoint_id=None):
    lock(session, p.tenant_id)
    fc = verified(session, result_id, "forecast")
    inp = verified(session, uuid.UUID(fc.payload["input_id"]), "input")
    cp = (
        require_checkpoint(session, truth_checkpoint_id)
        if truth_checkpoint_id
        else checkpoint(session, p.tenant_id)
    )
    if cp.captured_at < datetime.fromisoformat(fc.payload["as_of"]):
        raise InvalidInput("truth checkpoint precedes forecast issuance")
    identity = f"{fc.id}:{cp.id}"
    existing = session.scalar(
        select(DecisionArtifact).where(
            DecisionArtifact.kind == "evaluation", DecisionArtifact.identity == identity
        )
    )
    if existing:
        return existing
    observations = {}
    coverage = {}
    authority_match = False
    truth_error = None
    try:
        # Failures roll back ONLY the replay savepoint so pending/unknown labels
        # can be retained without poisoning the outer audit transaction.
        with session.begin_nested():
            with replay(session, cp) as provenance:
                from ..integration.observations import authorities, load_demand

                rules = authorities(session, p.tenant_id)
                original = inp.payload["watermarks"]["authorities"]

                def contract(r):
                    return (
                        str(r["location_id"]),
                        r["channel"],
                        str(r["connection_id"]),
                        r["stream"],
                        str(r.get("coverage_start")),
                        r.get("quantity_basis") or "base_units",
                    )

                current = [
                    {
                        "location_id": r.location_id,
                        "channel": r.channel,
                        "connection_id": r.connection_id,
                        "stream": r.stream,
                        "coverage_start": r.coverage_start,
                        "quantity_basis": r.quantity_basis,
                    }
                    for r in rules
                    if r.domain == "demand"
                ]
                authority_match = {
                    contract(r) for r in original if r["domain"] == "demand"
                } == {contract(r) for r in current}
                first = min(
                    date.fromisoformat(
                        r["horizons"][str(fc.payload["horizons"][0])]["target_start"]
                    )
                    for r in fc.payload["rows"]
                )
                observations, coverage = load_demand(session, p.tenant_id, rules, first)
    except InvalidInput as exc:
        provenance = {}
        truth_error = str(exc)
    from ..integration.observations import zero_is_proven

    labels = {}
    paired = []
    excluded = defaultdict(int)
    for row in fc.payload["rows"]:
        sku_id = uuid.UUID(inp.payload["ids"]["skus"][row["sku"]])
        loc_id = uuid.UUID(inp.payload["ids"]["locations"][row["warehouse_id"]])
        key = (sku_id, loc_id)
        for h, interval in row["horizons"].items():
            labelkey = f"{row['sku']}/{row['warehouse_id']}:{h}"
            if labelkey not in labels:
                lo = date.fromisoformat(interval["target_start"])
                hi = date.fromisoformat(interval["target_end"])
                tz = ZoneInfo(row["timezone"])
                maturity = datetime.combine(
                    hi + timedelta(days=1), time.min, tzinfo=tz
                ).astimezone(UTC)
                state = "mature"
                actual = 0
                if cp.captured_at < maturity:
                    state = "pending"
                elif truth_error or not authority_match or loc_id not in coverage:
                    state = "unknown_authority_or_coverage"
                else:
                    for i in range((hi - lo).days + 1):
                        day = lo + timedelta(days=i)
                        obs = observations.get(key, {}).get(
                            day,
                            {
                                "units": 0,
                                "censored": False,
                                "missing": False,
                                "rules": set(),
                            },
                        )
                        missing = obs["missing"] or any(
                            not zero_is_proven(rule, intervals, day, tz)
                            if rule.stream == "order_lines"
                            else rule.id not in obs["rules"]
                            for rule, intervals in coverage[loc_id]
                        )
                        if missing:
                            state = "missing"
                            break
                        if obs["censored"]:
                            state = "censored"
                            break
                        actual += obs["units"]
                labels[labelkey] = {
                    "status": state,
                    "actual": actual if state == "mature" else None,
                    "mature_at": maturity.isoformat(),
                }
            label = labels[labelkey]
            if label["status"] != "mature":
                excluded[label["status"]] += 1
                continue
            paired.append(
                {
                    "sku": row["sku"],
                    "warehouse_id": row["warehouse_id"],
                    "model": row["model"],
                    "horizon": int(h),
                    "origin": fc.payload["as_of"],
                    "target_start": interval["target_start"],
                    "target_end": interval["target_end"],
                    **{
                        k: interval[k]
                        for k in ("expected", "p50", "p80", "p95", "calibrated")
                    },
                    "actual": label["actual"],
                    "forecast_id": str(fc.id),
                    "calibration_id": fc.payload["calibration_id"],
                    "scope": fc.payload["scope"],
                    "code_digest": fc.payload["code_digest"],
                    "truth_checkpoint_id": str(cp.id),
                    "truth_available_at": cp.captured_at.isoformat(),
                }
            )
    actual_art = artifact(
        session,
        p.tenant_id,
        "actual",
        identity,
        {
            "forecast_id": str(fc.id),
            "labels": labels,
            "fact_revisions": provenance,
            "authority_match": authority_match,
            "error": truth_error,
        },
        checkpoint_id=cp.id,
        parent_id=fc.id,
    )
    grouped = defaultdict(list)
    for r in paired:
        grouped[(r["model"], r["horizon"])].append(r)
    report = {f"{m}:{h}": metrics(rows) for (m, h), rows in sorted(grouped.items())}
    mean = {h: metrics(rows) for (m, h), rows in grouped.items() if m == "mean_28"}
    for (m, h), rows in grouped.items():
        baseline = mean.get(h, {}).get("mae")
        report[f"{m}:{h}"]["skill_vs_mean28"] = (
            1 - metrics(rows)["mae"] / baseline if baseline else None
        )
    result = artifact(
        session,
        p.tenant_id,
        "evaluation",
        identity,
        {
            "forecast_id": str(fc.id),
            "actual_id": str(actual_art.id),
            "scope": fc.payload["scope"],
            "as_of": fc.payload["as_of"],
            "rows": paired,
            "metrics": report,
            "excluded": dict(excluded),
            "truth_checkpoint_id": str(cp.id),
            "truth_available_at": cp.captured_at,
            "gates": GATES,
        },
        checkpoint_id=cp.id,
        parent_id=fc.id,
        actor=p.user_id,
    )
    record_audit(
        session,
        p,
        "forecast.evaluate",
        "decision_artifact",
        result.id,
        after={"metrics": report, "excluded": dict(excluded)},
    )
    return result


def all_samples(session, spec):
    """Latest evaluated truth per prediction window; repeated checks never add samples.

    Distinct intraday issues are retained for trace, but statistical gate counts
    use target dates so reissuing the same day cannot inflate evidence volume.
    """
    # Rank in PostgreSQL before deserializing large JSON labels. A newer
    # censored/unknown evaluation must suppress an earlier usable one too.
    ranked = (
        select(
            DecisionArtifact.id,
            func.row_number()
            .over(
                partition_by=DecisionArtifact.payload["forecast_id"].astext,
                order_by=(
                    DecisionArtifact.created_at.desc(),
                    DecisionArtifact.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(
            DecisionArtifact.kind == "evaluation",
            DecisionArtifact.payload["scope"].astext == spec,
            DecisionArtifact.payload["forecast_id"].astext.is_not(None),
        )
        .subquery()
    )
    reports = session.scalars(
        select(DecisionArtifact)
        .join(ranked, ranked.c.id == DecisionArtifact.id)
        .where(ranked.c.rank == 1)
        .order_by(DecisionArtifact.created_at, DecisionArtifact.id)
        .execution_options(yield_per=10)
    )
    rows = []
    for e in reports:
        if len(rows) + len(e.payload["rows"]) > 1000000:
            raise InvalidInput(
                "evaluation sample budget exceeded; use a dedicated bounded offline evidence store"
            )
        rows.extend(r | {"evaluation_id": str(e.id)} for r in e.payload["rows"])
    return rows


def independent(rows):
    selected = []
    last = {}
    for r in sorted(rows, key=lambda r: (r["target_start"], r["origin"])):
        key = (r["sku"], r["warehouse_id"], r["model"], r["horizon"])
        if r["target_start"] <= last.get(key, ""):
            continue
        selected.append(r)
        last[key] = r["target_end"]
    return selected


def support(rows):
    return {
        "samples": len(rows),
        "origins": len({r["target_start"] for r in rows}),
        "series": len({(r["sku"], r["warehouse_id"]) for r in rows}),
    }


def enough(rows):
    n = support(independent(rows))
    return (
        n["samples"] >= GATES["minimum_samples"]
        and n["origins"] >= GATES["minimum_origins"]
        and n["series"] >= GATES["minimum_series"]
    )


def empirical_quantile(values, q):
    # Finite sample conformal rank, conservatively bounded at observed maximum.
    values = sorted(values)
    return values[min(len(values) - 1, max(0, ceil((len(values) + 1) * q) - 1))]


def calibrate(session, p, reference_id, model):
    lock(session, p.tenant_id)
    reference = verified(session, reference_id, "forecast")
    if model == "mean_28" or model not in reference.payload["models"]:
        raise InvalidInput("choose an evaluated candidate model")
    rows = [
        r
        for r in all_samples(session, reference.payload["scope"])
        if r["model"] == model and r["code_digest"] == reference.payload["code_digest"]
    ]
    if not rows:
        raise InvalidInput(
            "no mature observable evaluations; collect forward predictions first"
        )
    offsets = {}
    stats = {}
    ready = True
    for h in reference.payload["horizons"]:
        group = independent([r for r in rows if r["horizon"] == h])
        if not group:
            raise InvalidInput(
                "every requested horizon needs mature evaluation evidence"
            )
        offsets[f"{model}:{h}"] = [
            empirical_quantile([r["actual"] - r["expected"] for r in group], q)
            for q in (0.5, 0.8, 0.95)
        ]
        stats[str(h)] = support(group)
        ready &= enough(group)
    payload = {
        "scope": reference.payload["scope"],
        "model": model,
        "code_digest": reference.payload["code_digest"],
        "offsets": offsets,
        "support": stats,
        "ready": ready,
        "status": "eligible_for_forward_test"
        if ready
        else "shadow_only_insufficient_support",
        "fit_available_at": utcnow(),
        "calibration_samples": sorted(content_hash(r) for r in rows),
        "max_training_target_end": max(r["target_end"] for r in rows),
        "gates": GATES,
        "evaluation_ids": sorted({r["evaluation_id"] for r in rows}),
        "method": "signed cumulative residual split-conformal by horizon",
    }
    row = artifact(
        session,
        p.tenant_id,
        "calibration",
        uuid.uuid4().hex,
        payload,
        parent_id=reference.id,
        actor=p.user_id,
    )
    record_audit(
        session,
        p,
        "forecast.calibrate",
        "decision_artifact",
        row.id,
        after={"ready": ready, "support": stats},
    )
    return row


def job_reliability(session, cal):
    counts = dict(
        session.execute(
            select(ForecastJob.status, func.count())
            .where(
                ForecastJob.scope_key == cal.payload["scope"],
                ForecastJob.kernel_digest == cal.payload["code_digest"],
                ForecastJob.model_versions.contains([cal.payload["model"]]),
                ForecastJob.status.in_(("succeeded", "failed")),
            )
            .group_by(ForecastJob.status)
        ).all()
    )
    n = sum(counts.values())
    failed = counts.get("failed", 0)
    return {
        "completed": n,
        "failed": failed,
        "failure_rate": failed / n if n else None,
        "population": "same tenant/scope/kernel/model; explicit cancellation excluded",
    }


def promotion_report(session, cal, champion):
    samples = all_samples(session, cal.payload["scope"])
    model = cal.payload["model"]
    matched = []
    reasons = []
    groups = {}
    paired = {
        (
            r["forecast_id"],
            r["sku"],
            r["warehouse_id"],
            r["horizon"],
            r["target_start"],
            r["model"],
        ): r
        for r in samples
    }
    for r in samples:
        if (
            r["model"] != model
            or r["calibration_id"] != str(cal.id)
            or r["code_digest"] != cal.payload["code_digest"]
        ):
            continue
        # A calibration artifact was not available to any earlier issue. Test
        # labels must also begin strictly after the complete calibration period.
        if (
            datetime.fromisoformat(r["origin"]) <= cal.created_at
            or r["target_start"] <= cal.payload["max_training_target_end"]
        ):
            continue
        key = (
            r["forecast_id"],
            r["sku"],
            r["warehouse_id"],
            r["horizon"],
            r["target_start"],
        )
        baseline = paired.get((*key, "mean_28"))
        champ = paired.get((*key, champion))
        if baseline and champ and baseline["actual"] == champ["actual"] == r["actual"]:
            matched.append((r, baseline, champ))
    reliability = job_reliability(session, cal)
    if (
        reliability["completed"] < GATES["minimum_origins"]
        or reliability["failure_rate"] is None
        or reliability["failure_rate"] > GATES["maximum_job_failure_rate"]
    ):
        reasons.append("runtime_reliability_evidence_insufficient")
    if not cal.payload["ready"]:
        reasons.append("calibration_support_insufficient")
    if not matched:
        reasons.append("no_independent_forward_test_after_calibration")
    for h in sorted(int(k) for k in cal.payload["support"]):
        eligible = independent([t[0] for t in matched if t[0]["horizon"] == h])
        keys = {content_hash(r) for r in eligible}
        triples = [t for t in matched if content_hash(t[0]) in keys]
        rows = [t[0] for t in triples]
        m, b, c = [metrics([t[i] for t in triples]) for i in range(3)]
        fail = []
        if not enough(rows):
            fail.append("insufficient_independent_test_support")
        if not rows or m["mae"] >= c["mae"] * (1 - GATES["relative_mae_gain"]) - 1e-9:
            fail.append("champion_gain_below_2pct")
        if not rows or m["mae"] > b["mae"] + 1e-9:
            fail.append("mean28_regression")
        if m["bias"] is None or abs(m["bias"]) > GATES["maximum_abs_bias"]:
            fail.append("bias")
        if (
            m["p95_coverage"] is None
            or not GATES["coverage_low"] <= m["p95_coverage"] <= GATES["coverage_high"]
        ):
            fail.append("p95_coverage")
        by_warehouse = defaultdict(list)
        for t in triples:
            by_warehouse[t[0]["warehouse_id"]].append(t)
        warehouse_metrics = {}
        for wh, ts in by_warehouse.items():
            a, bm = metrics([t[0] for t in ts]), metrics([t[1] for t in ts])
            warehouse_metrics[wh] = {"candidate": a, "mean28": bm}
            if a["mae"] > bm["mae"] * (1 + GATES["maximum_group_regression"]) + 1e-9:
                fail.append(f"warehouse_regression:{wh}")
        groups[str(h)] = {
            "candidate": m,
            "mean28": b,
            "champion": c,
            "support": support(rows),
            "warehouse_metrics": warehouse_metrics,
            "failures": fail,
        }
        reasons.extend(f"{h}:{r}" for r in fail)
    # Synthetic/mock source evidence cannot promote a real execution model.
    for rid in {r["forecast_id"] for r, _, _ in matched}:
        fc = verified(session, uuid.UUID(rid), "forecast")
        inp = verified(session, uuid.UUID(fc.payload["input_id"]), "input")
        if inp.payload.get("stale"):
            reasons.append("stale_input_in_test")
        # Source identities are frozen, not today's connection flags.
        from .history import facts, require_checkpoint

        cp = require_checkpoint(session, inp.checkpoint_id)
        selected = {
            str(r["connection_id"]) for r in inp.payload["watermarks"]["authorities"]
        }
        market = inp.payload["watermarks"].get("market_source")
        if market:
            selected.add(str(market["connection_id"]))
        selected.update(str(r["connection_id"]) for r in inp.payload["watermarks"].get("external_signal_sources", []))
        if any(
            str(c["id"]) in selected and c.get("is_mock")
            for c, _ in facts(session, cp, "integration_connection")
        ):
            reasons.append("mock_evidence_ineligible_for_execution")
    return {
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "horizons": groups,
        "gates": GATES,
        "calibration_id": str(cal.id),
        "scope": cal.payload["scope"],
        "model": model,
        "champion": champion,
        "evaluation_ids": sorted({r["evaluation_id"] for t in matched for r in t}),
        "job_reliability": reliability,
    }


def champion_for(session, spec):
    from ..models import PolicyVersion

    policy = session.scalar(select(PolicyVersion).where(PolicyVersion.is_active))
    ident = (policy.config if policy else {}).get("model_release_id")
    if ident:
        release = session.get(DecisionCase, uuid.UUID(ident))
        if release and release.kind == "model_release" and release.status == "active":
            return release.payload["model"]
    for release in session.scalars(
        select(DecisionCase).where(
            DecisionCase.kind == "model_release", DecisionCase.status == "active"
        )
    ):
        if release.payload["scope"] == spec:
            return release.payload["model"]
    return "seasonal-tsb-v2"


def propose_release(session, p, calibration_id, champion):
    lock(session, p.tenant_id)
    cal = verified(session, calibration_id, "calibration")
    if champion == cal.payload["model"]:
        raise InvalidInput("candidate and champion must differ")
    report = promotion_report(session, cal, champion)
    evidence = artifact(
        session,
        p.tenant_id,
        "promotion_evidence",
        uuid.uuid4().hex,
        report,
        parent_id=cal.id,
        actor=p.user_id,
    )
    row = DecisionCase(
        tenant_id=p.tenant_id,
        kind="model_release",
        scope_key=uuid.uuid4().hex,
        status="pending" if report["eligible"] else "blocked",
        created_by=p.user_id,
        artifact_id=evidence.id,
        payload={
            "calibration_id": str(cal.id),
            "model": cal.payload["model"],
            "champion": champion,
            "scope": cal.payload["scope"],
        },
    )
    session.add(row)
    session.flush()
    record_audit(session, p, "model.propose", "decision_case", row.id, after=report)
    return row


def decide_release(session, p, ident, revision, action, note):
    lock(session, p.tenant_id)
    row = case_for_update(session, ident, "model_release", revision)
    if not note.strip():
        raise InvalidInput("release decision reason required")
    if action == "activate":
        if row.status != "pending":
            raise Conflict("only eligible pending releases can activate")
        champion = champion_for(session, row.payload["scope"])
        from .jobs import code_digest

        cal = verified(session, uuid.UUID(row.payload["calibration_id"]), "calibration")
        if cal.payload["code_digest"] != code_digest():
            raise Conflict("activate using the exact validated code image")
        if champion != row.payload["champion"]:
            raise Conflict(
                "active champion changed; request a fresh paired promotion review"
            )
        separate_approver(row, p)
        cal = verified(session, uuid.UUID(row.payload["calibration_id"]), "calibration")
        report = promotion_report(session, cal, row.payload["champion"])
        if not report["eligible"]:
            raise Conflict("promotion gates no longer pass", details=report)
        for other in session.scalars(
            select(DecisionCase).where(
                DecisionCase.kind == "model_release", DecisionCase.status == "active"
            )
        ):
            if other.payload["scope"] == row.payload["scope"]:
                other.status = "retired"
                other.revision += 1
        row.status = "active"
        row.approved_by = p.user_id
    elif action == "rollback":
        if row.status != "active":
            raise Conflict("only active releases can roll back")
        row.status = "rolled_back"
    else:
        raise InvalidInput("unknown model release action")
    row.revision += 1
    record_audit(
        session,
        p,
        "model." + action,
        "decision_case",
        row.id,
        after={"status": row.status, "note": note},
    )
    return row
