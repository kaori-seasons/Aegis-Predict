"""Opt-in challenger: purged inner validation, issue-lag regression, grouped cold start.

This kernel has no DB/network access. All inputs and feature versions are frozen.
Long horizons beyond the validated direct horizon use the v3 daily trajectory.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from math import cos, pi, sin
from statistics import fmean, median, pstdev

import numpy as np

from .commerce_features import VERSION, features, launch_events, local_issue, stamp
from .forecast import MODEL_VERSION, masks, uncensor

MODEL = "commerce-coldstart-v5"
MAX_DIRECT_HORIZON = 28
MIN_DONOR_GROUPS = 3


def prefix(series, day):
    n = max(
        0,
        min(
            len(series.daily_units),
            (day - date.fromisoformat(series.calendar_start)).days,
        ),
    )
    return replace(
        series,
        daily_units=series.daily_units[:n],
        missing_days=(series.missing_days or [False] * len(series.daily_units))[:n],
        stockout_days=(series.stockout_days or [False] * len(series.daily_units))[:n],
    )


def lifecycle(context, series, origin):
    day = (
        stamp(origin)
        .astimezone(__import__("zoneinfo").ZoneInfo(series.timezone))
        .date()
    )
    events = [r for r in launch_events(context, origin) if r["sku"] == series.sku]
    first = next((r for r in events if r["event_kind"] == "first_sale"), None)
    planned = next((r for r in events if r["event_kind"] == "planned_launch"), None)
    event = first or planned
    if event is None:
        return {"stage": "unknown_launch", "age": None, "event": None}
    age = (day - date.fromisoformat(str(event["event_date"]))).days
    stage = (
        ("scheduled" if age < 0 else "awaiting_first_sale")
        if not first
        else ("early" if age < 7 else "incubating" if age < 28 else "established")
    )
    return {"stage": stage, "age": age, "event": event}


def cold_curve(network, series, horizon, origin, heldout_groups=()):
    context = network.commerce_context
    life = lifecycle(context, series, origin)
    target = life["event"]
    if not target or life["stage"] == "established":
        return None, life
    excluded = set(heldout_groups) | {target["launch_group"]}
    events = {
        r["sku"]: r
        for r in launch_events(context, origin)
        if r["event_kind"] == "first_sale"
    }
    day = (
        stamp(origin)
        .astimezone(__import__("zoneinfo").ZoneInfo(series.timezone))
        .date()
    )
    donors = {}
    donor_ids = []
    for peer in network.demand_history:
        event = events.get(peer.sku)
        if (
            not event
            or peer.warehouse_id != series.warehouse_id
            or event["cohort_key"] != target["cohort_key"]
            or event["launch_group"] in excluded
        ):
            continue
        peer = prefix(peer, day)
        missing, censored = masks(peer)
        start = date.fromisoformat(peer.calendar_start)
        launch = date.fromisoformat(str(event["event_date"]))
        # Require every target age in a group's donor profile; do not extrapolate launch tails.
        profile = []
        for step in range(horizon):
            age = life["age"] + step
            index = (launch + timedelta(days=age) - start).days
            if age < 0:
                profile.append(0.0)
            elif (
                0 <= index < len(peer.daily_units)
                and not missing[index]
                and not censored[index]
            ):
                profile.append(float(peer.daily_units[index]))
            else:
                break
        if len(profile) == horizon:
            donors.setdefault(event["launch_group"], []).append(profile)
            donor_ids.append(event["id"])
    info = {
        **life,
        "donor_groups": sorted(donors),
        "donor_event_ids": sorted(set(donor_ids)),
        "excluded_groups": sorted(excluded),
        "cohort_scope": "same-warehouse-explicit-launch-cohort",
        "minimum_groups": MIN_DONOR_GROUPS,
    }
    if len(donors) < MIN_DONOR_GROUPS:
        return None, {**info, "reason": "insufficient_mature_donor_groups"}
    profiles = [
        [median(p[i] for p in group) for i in range(horizon)]
        for group in donors.values()
    ]
    daily = [median(p[i] for p in profiles) for i in range(horizon)]
    own = prefix(series, day)
    missing, censored = masks(own)
    start = date.fromisoformat(own.calendar_start)
    launch = date.fromisoformat(str(target["event_date"]))
    observed = [
        v
        for i, v in enumerate(own.daily_units)
        if start + timedelta(days=i) >= launch and not missing[i] and not censored[i]
    ]
    # Limited observed-history shrinkage; the prior is never fit on the held-out launch group.
    weight = min(0.5, len(observed) / 56) if target["event_kind"] == "first_sale" else 0
    if observed:
        daily = [
            (1 - weight) * v + weight * fmean(observed[-7:])
            if life["age"] + i >= 0
            else 0
            for i, v in enumerate(daily)
        ]
    return daily, {
        **info,
        "reason": "age_aligned_group_median",
        "own_history_weight": weight,
    }


def vector(snapshot, day, step):
    v = [snapshot["values"][k] for k in sorted(snapshot["values"])]
    # Imputation + missing indicators are fitted from training data below.
    weekday = day.weekday() * 2 * pi / 7
    return v + [
        sin(weekday),
        cos(weekday),
        step / 28,
        snapshot["known_holidays"].get(str(day)),
    ]


def fit(x, y):
    x = np.array([[np.nan if v is None else v for v in row] for row in x], dtype=float)
    # No nanmedian warnings for entirely absent columns. Median/scaling use training only.
    med = np.array(
        [
            np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0
            for col in x.T
        ]
    )
    missing = ~np.isfinite(x)
    x = np.where(missing, med, x)
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-9] = 1
    z = np.c_[np.ones(len(x)), (x - mean) / scale, missing.astype(float)]
    penalty = np.eye(z.shape[1]) * 10
    penalty[0, 0] = 1e-8
    weights = np.linalg.solve(z.T @ z + penalty, z.T @ np.asarray(y))
    return med, mean, scale, weights


def apply(model, x):
    med, mean, scale, weights = model
    x = np.array([[np.nan if v is None else v for v in row] for row in x], dtype=float)
    missing = ~np.isfinite(x)
    z = np.c_[
        np.ones(len(x)),
        (np.where(missing, med, x) - mean) / scale,
        missing.astype(float),
    ]
    return np.maximum(0, z @ weights).tolist()


def candidate(network, series, horizon, origin, *, heldout_groups=()):
    from ..decision.forecasting import curve

    origin = stamp(origin)
    day = origin.astimezone(__import__("zoneinfo").ZoneInfo(series.timezone)).date()
    series = prefix(series, day)
    baseline, evidence = curve(series, MODEL_VERSION, horizon, [], origin.isoformat())
    values, _ = uncensor(series)
    life = lifecycle(network.commerce_context, series, origin)
    daily, cold = cold_curve(network, series, horizon, origin, heldout_groups)
    meta = {
        "model_name": MODEL,
        "feature_version": VERSION,
        "volatility": pstdev(values[-56:]) if len(values) > 1 else 0,
        "lifecycle": cold,
        "selection": {"reason": "v3_fallback"},
        "execution_blockers": [],
    }
    if daily is not None:
        return daily, {
            **meta,
            "selection": {"reason": "cold_start_group_prior"},
            "execution_blockers": ["cold_start_requires_separate_forward_validation"],
        }
    if life["stage"] != "established" and life["event"]:
        return baseline, {
            **meta,
            "selection": {"reason": cold.get("reason", "cold_start_unknown")},
            "execution_blockers": ["insufficient_cold_start_support"],
        }
    snapshot = features(
        network.commerce_context,
        series.sku,
        series.warehouse_id,
        origin,
        series.timezone,
    )
    meta["features"] = snapshot
    if not any(v for k, v in snapshot["values"].items() if k.endswith("_coverage")):
        return baseline, {**meta, "selection": {"reason": "no_available_lag_features"}}
    h = min(horizon, MAX_DIRECT_HORIZON)
    n = len(series.daily_units)
    start = date.fromisoformat(series.calendar_start)
    if n < 112:
        return baseline, {
            **meta,
            "selection": {"reason": "insufficient_training_history"},
        }
    missing, censored = masks(series)
    snapshots = {}

    def snap(i):
        if i not in snapshots:
            snapshots[i] = features(
                network.commerce_context,
                series.sku,
                series.warehouse_id,
                local_issue(start + timedelta(days=i), series.timezone),
                series.timezone,
            )
        return snapshots[i]

    def without_external(row):
        # Same calendar/lead model, with all commerce/public values removed.
        return [None] * (len(row) - 4) + row[-4:-1] + [None]

    def train(end, ablated=False):
        x = []
        y = []
        # All training target dates precede end, purging overlapping validation targets.
        for i in range(max(28, end - 120), end - h + 1, 2):
            ss = snap(i)
            if not any(v for k, v in ss["values"].items() if k.endswith("_coverage")):
                continue
            for step in range(h):
                j = i + step
                if missing[j] or censored[j]:
                    continue
                row = vector(ss, start + timedelta(days=j), step + 1)
                x.append(without_external(row) if ablated else row)
                y.append(series.daily_units[j])
        return fit(x, y) if len(y) >= 56 else None

    split = n - h - 21
    inner = train(split)
    ablated = train(split, ablated=True)
    if inner is None:
        return baseline, {
            **meta,
            "selection": {"reason": "insufficient_point_in_time_training_features"},
        }
    losses = []
    for i in (split, split + 7, split + 14):
        if i + h > n:
            continue
        if any(missing[j] or censored[j] for j in range(i, i + h)):
            continue
        predictions = apply(
            inner,
            [vector(snap(i), start + timedelta(days=i + j), j + 1) for j in range(h)],
        )
        base, _ = curve(
            prefix(series, start + timedelta(days=i)),
            MODEL_VERSION,
            h,
            [],
            local_issue(start + timedelta(days=i), series.timezone).isoformat(),
        )
        ablation_predictions = apply(
            ablated,
            [
                without_external(vector(snap(i), start + timedelta(days=i + j), j + 1))
                for j in range(h)
            ],
        )
        actual = series.daily_units[i : i + h]
        losses.append(
            {
                "origin": str(start + timedelta(days=i)),
                "candidate_mae": fmean(abs(a - b) for a, b in zip(predictions, actual)),
                "v3_mae": fmean(abs(a - b) for a, b in zip(base, actual)),
                "without_external_mae": fmean(
                    abs(a - b) for a, b in zip(ablation_predictions, actual)
                ),
            }
        )
    gain = (
        1 - sum(r["candidate_mae"] for r in losses) / sum(r["v3_mae"] for r in losses)
        if losses and sum(r["v3_mae"] for r in losses) > 0
        else None
    )
    external_gain = (
        1
        - sum(r["candidate_mae"] for r in losses)
        / sum(r["without_external_mae"] for r in losses)
        if losses and sum(r["without_external_mae"] for r in losses) > 0
        else None
    )
    selection = {
        "reason": "inner_gate_rejected",
        "external_gain": external_gain,
        "purged_training_end": str(start + timedelta(days=split)),
        "folds": losses,
        "gain": gain,
        "required_gain": 0.02,
        "minimum_folds": 3,
        "max_direct_horizon": h,
    }
    meta["selection"] = selection
    if (
        len(losses) != 3
        or gain is None
        or gain < 0.02
        or external_gain is None
        or external_gain < 0.02
        or sum(r["candidate_mae"] < r["v3_mae"] for r in losses) < 2
    ):
        return baseline, meta
    model = train(n)
    predictions = apply(
        model, [vector(snapshot, day + timedelta(days=j), j + 1) for j in range(h)]
    )
    # Training-only amplitude guard; no future observations or public observed values are used.
    cap = max(1, max(values[-120:], default=0) * 3)
    daily = [min(cap, v) for v in predictions] + baseline[h:]
    meta["selection"] = {
        **selection,
        "reason": "purged_inner_validation_passed",
        "tail": "v3_after_day28",
    }
    return daily, meta
