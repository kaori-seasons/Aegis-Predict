"""Versioned market challenger. Every fit and inner decision rebuilds its own as-of view.

Only planned covariates known to the system at that origin are predictors. Observed
same-day metrics, missing metadata and revisions received later never enter a fit.
"""

from __future__ import annotations
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from math import ceil, isfinite, log, sqrt
from statistics import fmean
from zoneinfo import ZoneInfo
import numpy as np
from ..core.util import content_hash
from .forecast import (
    MARKET_MODEL_VERSION,
    MODEL_VERSION,
    INNER_MIN_FOLDS,
    INNER_MAX_FOLDS,
    MIN_RELATIVE_GAIN,
    forecast,
    masks,
    prefix,
)

FEATURES = (
    "ad_spend_index",
    "promotion_lift",
    "seasonal_index",
    "platform_event_lift",
    "price_index",
)
RIDGE = 3.0
MIN_MARKET_TRAIN = 56
TRAIN_WINDOW = 112
MIN_COVERAGE = 0.8


def timestamp(value):
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(
            "as-of and publication timestamps require explicit timezone offsets"
        )
    return result.astimezone(UTC)


def origin_at(series, end=None):
    if not series.calendar_start:
        return None
    day = date.fromisoformat(series.calendar_start) + timedelta(
        days=len(series.daily_units) if end is None else end
    )
    return datetime.combine(day, time.min, ZoneInfo(series.timezone)).astimezone(UTC)


def asof_signals(series, revisions, cutoff):
    """Filter availability BEFORE ranking. Tombstones suppress older values.

    Input must already be scoped to one approved provider/platform by the assembler.
    Offline callers get fail-closed ambiguity detection rather than provider averaging.
    """
    if cutoff is None:
        return {}, []
    cutoff = timestamp(cutoff)
    eligible = []
    for r in revisions:
        if (
            r.get("sku") != series.sku
            or r.get("warehouse_id") != series.warehouse_id
            or r.get("signal_kind") != "planned"
        ):
            continue
        # Unknown historical availability is not inferred from source_updated_at.
        if not all(
            r.get(k)
            for k in (
                "published_at",
                "received_at",
                "available_at",
                "source_system",
                "platform",
            )
        ):
            continue
        published, received, available = (
            timestamp(r[k]) for k in ("published_at", "received_at", "available_at")
        )
        if max(published, received, available) > cutoff:
            continue
        if available < max(published, received):
            raise ValueError("invalid market availability ordering")
        revision = r.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid market revision")
        day = date.fromisoformat(r["signal_date"])
        if (
            not all(
                type(r.get(k)) in (int, float) and isfinite(r[k]) and r[k] >= 0
                for k in FEATURES
            )
            or r["price_index"] <= 0
        ):
            raise ValueError("invalid market indices")
        eligible.append((day, revision, r))
    if len({(r["source_system"], r["platform"]) for _, _, r in eligible}) > 1:
        raise ValueError(
            "market model requires one explicitly selected provider/platform per series"
        )
    latest = {}
    for day, revision, r in eligible:
        previous = latest.get(day)
        if (
            previous
            and previous["revision"] == revision
            and content_hash(previous) != content_hash(r)
        ):
            raise ValueError("conflicting immutable market revision")
        if previous is None or revision > previous["revision"]:
            latest[day] = r
    rows = [latest[k] for k in sorted(latest)]
    return {day: r for day, r in latest.items() if not r.get("is_deleted", False)}, rows


def _candidate(series, horizon, revisions, cutoff, *, include_daily=False):
    """Fixed ridge fit. Scaling, coefficients and training masks use this prefix only."""
    start = date.fromisoformat(series.calendar_start)
    signals, chosen = asof_signals(series, revisions, cutoff)
    n = len(series.daily_units)
    missing, stockout = masks(series)
    observed = [
        i for i in range(max(0, n - TRAIN_WINDOW), n) if not (missing[i] or stockout[i])
    ]
    train = [i for i in observed if start + timedelta(days=i) in signals]
    future = [i for i in range(n, n + horizon) if start + timedelta(days=i) in signals]
    evidence = {
        "training_rows": len(train),
        "training_coverage": len(train) / max(1, len(observed)),
        "future_coverage": len(future) / horizon,
        "as_of": timestamp(cutoff).isoformat(),
        "signal_digest": content_hash(chosen),
        "signal_revisions": [r.get("id", content_hash(r)) for r in chosen],
    }
    if (
        len(train) < MIN_MARKET_TRAIN
        or len(train) / max(1, len(observed)) < MIN_COVERAGE
        or len(future) / horizon < MIN_COVERAGE
    ):
        return None, evidence | {"reason": "insufficient_market_coverage"}

    def features(i):
        row = signals.get(start + timedelta(days=i))
        return [log(max(0.05, min(4.0, row[k]))) if row else 0.0 for k in FEATURES]

    x = np.array([features(i) for i in train])
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    if float(scale.max()) < 0.05:
        return None, evidence | {"reason": "no_market_variation"}
    scale = np.maximum(scale, 0.05)

    def matrix(indices):
        return np.array(
            [
                [
                    1.0,
                    (i - (n - 1)) / 28.0,
                    *[(start + timedelta(days=i)).weekday() == d for d in range(6)],
                    *((np.array(features(i)) - mean) / scale),
                ]
                for i in indices
            ],
            dtype=float,
        )

    design = matrix(train)
    y = np.array([series.daily_units[i] for i in train], dtype=float)
    penalty = np.eye(design.shape[1]) * RIDGE
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    limit = max(1.0, 2 * max(y))
    predicted = np.clip(matrix(list(range(n, n + horizon))) @ beta, 0.0, limit)
    # Unavailable future covariates are NOT silently assigned observed future values.
    # Coverage permits up to20% gaps, whose forecasts retain the univariate estimate.
    base = forecast(series, horizon).daily_rate
    for j in range(horizon):
        if n + j not in future:
            predicted[j] = base
    evidence.update(
        reason="market_ridge_fit",
        coefficients=[round(float(v), 8) for v in beta],
        feature_mean=mean.tolist(),
        feature_scale=scale.tolist(),
        ridge=RIDGE,
    )
    if include_daily:
        evidence["daily_predictions"] = [float(v) for v in predicted]
    return float(predicted.mean()), evidence


def market_forecast(series, horizon, *, revisions=(), as_of=None, fixed=False):
    base = forecast(series, horizon, model_version=MODEL_VERSION)
    clock = timestamp(as_of) if as_of is not None else origin_at(series)
    evidence = {
        "reason": "missing_market_calendar",
        "univariate_model_version": MODEL_VERSION,
        "univariate_selection": base.selection,
        "market_enabled": False,
    }

    def output(rate=None):
        if rate is None:
            return replace(base, model_version=MARKET_MODEL_VERSION, selection=evidence)
        mean = rate * horizon
        sigma = base.volatility * sqrt(horizon)
        return replace(
            base,
            daily_rate=round(rate, 6),
            p50_units=ceil(mean),
            p80_units=ceil(mean + 0.841621 * sigma),
            p95_units=ceil(mean + 1.644854 * sigma),
            model_name="market_ridge",
            model_version=MARKET_MODEL_VERSION,
            selection=evidence | {"market_enabled": True},
        )

    if clock is None or not series.calendar_start:
        return output()
    if clock < origin_at(series):
        raise ValueError(
            "market forecast as_of precedes the end of its training calendar"
        )
    rate, fit = _candidate(series, horizon, revisions, clock)
    evidence.update(fit)
    if rate is None:
        return output()
    if fixed:
        return output(rate)
    n = len(series.daily_units)
    missing, stockout = masks(series)
    origins = []
    market_errors = []
    base_errors = []
    inner = []
    for origin in range(n - horizon, MIN_MARKET_TRAIN - 1, -horizon):
        if any(missing[origin : origin + horizon]) or any(
            stockout[origin : origin + horizon]
        ):
            continue
        train = prefix(series, origin)
        cutoff = origin_at(train)
        predicted, detail = _candidate(train, horizon, revisions, cutoff)
        if predicted is None:
            continue
        actual = sum(series.daily_units[origin : origin + horizon])
        market_errors.append(abs(predicted * horizon - actual))
        base_errors.append(abs(forecast(train, horizon).daily_rate * horizon - actual))
        origins.append(origin)
        inner.append(
            {
                "origin": origin,
                "end_exclusive": origin + horizon,
                "as_of": cutoff.isoformat(),
                "signal_digest": detail["signal_digest"],
            }
        )
        if len(origins) == INNER_MAX_FOLDS:
            break
    evidence.update(
        inner_origins=origins,
        inner_horizon=horizon,
        training_end_exclusive=n,
        inner_market_evidence=inner,
        minimum_folds=INNER_MIN_FOLDS,
        minimum_relative_gain=MIN_RELATIVE_GAIN,
    )
    if len(origins) < INNER_MIN_FOLDS:
        evidence["reason"] = "insufficient_market_inner_folds"
        return output()
    market_error = fmean(market_errors)
    base_error = fmean(base_errors)
    evidence.update(
        market_validation_mae=market_error, univariate_validation_mae=base_error
    )
    if (
        market_error < base_error * (1 - MIN_RELATIVE_GAIN) - 1e-9
        and sum(a < b - 1e-9 for a, b in zip(market_errors, base_errors))
        > len(origins) / 2
    ):
        evidence["reason"] = "market_training_prefix_validation_gain"
        return output(rate)
    evidence["reason"] = "market_not_clearly_better"
    return output()
