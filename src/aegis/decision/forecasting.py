"""One model selection, a frozen daily trajectory, horizon-specific cumulative evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from math import sqrt
from statistics import fmean, pstdev

from overseas_inventory.models import network_from_payload

from ..core.util import content_hash
from ..planning.forecast import (
    CANDIDATES,
    LEGACY_MODEL_VERSION,
    MARKET_MODEL_VERSION,
    MODEL_VERSION,
    _rate,
    forecast,
    uncensor,
)

TRAJECTORY_VERSION = "fixed-selection-daily-v1"
MODELS = (
    "mean_28",
    LEGACY_MODEL_VERSION,
    MODEL_VERSION,
    MARKET_MODEL_VERSION,
    "commerce-coldstart-v5",
)


def scope(input_payload, horizons):
    policy = input_payload["policy"]
    spec = {
        "trajectory": TRAJECTORY_VERSION,
        "max_horizon": max(horizons),
        "horizons": sorted(horizons),
        "history_days": policy["history_days"],
        "market_source": policy.get("market_signal_source"),
        "market_platform": policy.get("market_signal_platform"),
        "calendar": "local-completed-days",
        "quantity_basis": "base-units-single-level-bom-v1",
        "series_universe": sorted(
            (r["sku"], r["warehouse_id"], r.get("timezone", "UTC"))
            for r in input_payload["network"]["demand_history"]
        ),
        "demand_contracts": [
            {
                k: r.get(k)
                for k in (
                    "location_id",
                    "channel",
                    "connection_id",
                    "stream",
                    "coverage_start",
                    "quantity_basis",
                )
            }
            for r in input_payload.get("watermarks", {}).get("authorities", [])
            if r["domain"] == "demand"
        ],
        "bom_knowledge": input_payload.get("fact_revisions", {}).get(
            "bom_versions", []
        ),
    }
    context = input_payload["network"].get("commerce_context", {})
    if any(
        context.get(k)
        for k in (
            "commerce_performance",
            "sku_launches",
            "public_signals",
            "public_signal_bindings",
        )
    ) or any(
        context.get("config", {}).get(k)
        for k in (
            "commerce_signal_source",
            "launch_signal_source",
            "public_signal_source",
        )
    ):
        spec["commerce_contract"] = {
            "version": "commerce-lag-contract-v1",
            "config": context.get("config", {}),
            "cold_scope": "same-warehouse-explicit-launch-cohort",
            "public_bindings": sorted(
                {
                    tuple(
                        str(r.get(k))
                        for k in (
                            "sku",
                            "warehouse_id",
                            "provider",
                            "series_key",
                            "metric",
                            "effective_from",
                            "revision",
                            "is_deleted",
                        )
                    )
                    for r in context.get("public_signal_bindings", [])
                }
            ),
            "cohorts": sorted(
                set(
                    (r["sku"], r["launch_group"], r["cohort_key"])
                    for r in context.get("sku_launches", [])
                )
            ),
        }
    return content_hash(spec)


def curve(series, version, horizon, revisions, as_of, *, cold_start_rate=0.0):
    values, _ = uncensor(series)
    if version == "mean_28":
        rate = fmean(values[-28:]) if values else 0.0
        return [rate] * horizon, {
            "model_name": "mean_28",
            "selection": {
                "reason": "fixed_baseline",
                "training_end_exclusive": len(values),
            },
            "volatility": pstdev(values[-56:]) if len(values) > 1 else sqrt(rate),
        }
    fc = forecast(
        series,
        horizon,
        model_version=version,
        market_revisions=revisions,
        as_of=as_of,
        cold_start_rate=cold_start_rate if version == LEGACY_MODEL_VERSION else 0.0,
    )
    if version == MARKET_MODEL_VERSION and fc.selection.get("market_enabled"):
        from ..planning.market_forecast import _candidate, timestamp

        _, detail = _candidate(
            series, horizon, revisions, timestamp(as_of), include_daily=True
        )
        daily = detail.get("daily_predictions")
        if daily is not None:
            return daily, fc.to_dict()
    selected = fc.model_name
    if version == MARKET_MODEL_VERSION:
        selected = forecast(series, horizon, model_version=MODEL_VERSION).model_name
    if selected not in CANDIDATES:
        return [fc.daily_rate] * horizon, fc.to_dict()
    previous = 0.0
    daily = []
    for h in range(1, horizon + 1):
        total = _rate(series, values, h, selected) * h
        # Clipped trend candidates can produce falling cumulative totals. Project
        # to nondecreasing cumulative demand once, consistently for every horizon.
        daily.append(max(0.0, total - previous))
        previous += daily[-1]
    return daily, fc.to_dict()


def predict(input_payload, horizons, models, calibration=None):
    network = network_from_payload(input_payload["network"])
    signals = defaultdict(list)
    for r in network.market_signal_revisions:
        signals[(r["sku"], r["warehouse_id"])].append(r)
    category_rates = defaultdict(list)
    for series in network.demand_history:
        from ..planning.forecast import masks

        missing, censored = masks(series)
        seen = [
            v for v, m, c in zip(series.daily_units, missing, censored) if not (m or c)
        ]
        if len(seen) >= 7:
            category_rates[network.skus[series.sku].category].append(fmean(seen[-28:]))
    spec = scope(input_payload, horizons)
    rows = []
    for series in sorted(network.demand_history, key=lambda r: (r.sku, r.warehouse_id)):
        start = date.fromisoformat(series.calendar_start) + timedelta(
            days=len(series.daily_units)
        )
        for model in models:
            if model == "commerce-coldstart-v5":
                from ..planning.commerce_forecast import candidate

                daily, evidence = candidate(
                    network, series, max(horizons), network.forecast_as_of
                )
            else:
                daily, evidence = curve(
                    series,
                    model,
                    max(horizons),
                    signals[(series.sku, series.warehouse_id)],
                    network.forecast_as_of,
                    cold_start_rate=fmean(
                        category_rates[network.skus[series.sku].category]
                    )
                    if category_rates[network.skus[series.sku].category]
                    else 0.0,
                )
            cumulative = []
            total = 0.0
            for v in daily:
                total += v
                cumulative.append(total)
            intervals = {}
            for h in horizons:
                expected = cumulative[h - 1]
                offsets = (calibration or {}).get("offsets", {}).get(f"{model}:{h}")
                quantiles = (
                    [max(0.0, expected + o) for o in offsets]
                    if offsets
                    else [
                        expected,
                        expected + 0.841621 * evidence.get("volatility", 0) * sqrt(h),
                        expected + 1.644854 * evidence.get("volatility", 0) * sqrt(h),
                    ]
                )
                quantiles = sorted(quantiles)
                intervals[str(h)] = {
                    "expected": expected,
                    "p50": quantiles[0],
                    "p80": quantiles[1],
                    "p95": quantiles[2],
                    "calibrated": offsets is not None,
                    "target_start": str(start),
                    "target_end": str(start + timedelta(days=h - 1)),
                }
            monotone_intervals(intervals)
            rows.append(
                {
                    "sku": series.sku,
                    "warehouse_id": series.warehouse_id,
                    "timezone": series.timezone,
                    "model": model,
                    "daily": daily,
                    "horizons": intervals,
                    "evidence": evidence,
                }
            )
    return {
        "schema": TRAJECTORY_VERSION,
        "scope": spec,
        "horizons": horizons,
        "models": models,
        "rows": rows,
    }


def metrics(rows):
    if not rows:
        return {
            "count": 0,
            "mae": None,
            "wape": None,
            "bias": None,
            "p95_coverage": None,
            "p95_pinball": None,
        }
    n = len(rows)
    actual = sum(r["actual"] for r in rows)
    error = sum(abs(r["expected"] - r["actual"]) for r in rows)
    return {
        "count": n,
        "calibrated_count": sum(bool(r.get("calibrated")) for r in rows),
        "mae": error / n,
        "wape": error / actual if actual else None,
        "bias": sum(r["expected"] - r["actual"] for r in rows) / actual
        if actual
        else None,
        "p95_coverage": sum(r["actual"] <= r["p95"] for r in rows) / n,
        "p95_pinball": sum(
            (0.95 if r["actual"] >= r["p95"] else -0.05) * (r["actual"] - r["p95"])
            for r in rows
        )
        / n,
    }


def required_horizons(payload, requested):
    review = payload["policy"]["review_period_days"]
    leads = [r["lead_time_days"] for r in payload["network"]["lanes"]] + [
        r["lead_time_days"] for r in payload["network"]["transport_options"]
    ]
    values = sorted(
        set(requested + [d for l in leads for d in (max(1, l), l + review)])
    )
    if not values or len(values) > 24 or min(values) < 1 or max(values) > 365:
        raise ValueError(
            "at most24 horizons in [1,365], including every route protection period"
        )
    return values


def monotone_intervals(intervals):
    """Conservative cumulative envelope; retain raw values for scored diagnosis."""
    previous = [0.0, 0.0, 0.0]
    for h in sorted(intervals, key=int):
        interval = intervals[h]
        raw = [interval[k] for k in ("p50", "p80", "p95")]
        interval["raw_quantiles"] = dict(zip(("p50", "p80", "p95"), raw))
        revised = [
            max(previous[0], raw[0]),
            max(previous[1], raw[1]),
            max(previous[2], raw[2]),
        ]
        revised[1] = max(revised[0], revised[1])
        revised[2] = max(revised[1], revised[2])
        interval.update(zip(("p50", "p80", "p95"), revised))
        interval["horizon_envelope_adjusted"] = revised != raw
        interval["horizon_envelope_method"] = (
            "nondecreasing cumulative max; scored after correction"
        )
        previous = revised
