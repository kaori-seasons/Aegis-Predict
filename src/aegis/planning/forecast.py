"""Univariate forecasting with training-prefix-only model selection.

The mean28 fallback, fixed candidate grid and inner validation rules are versioned.
Outer evaluation outcomes never enter this module. Quantiles remain normal approximations.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass, field
from math import ceil, isfinite, sqrt
from statistics import fmean, pstdev
from overseas_inventory.models import DemandSeries
from .forecast_legacy import legacy_forecast

MODEL_VERSION = "rolling-champion-v3"
LEGACY_MODEL_VERSION = "seasonal-tsb-v2"
CANDIDATES = (
    "mean_28",
    "mean_7",
    "mean_56",
    "seasonal_naive_7",
    "weekday_mean_8",
    "ses_02",
    "damped_trend_28",
    "tsb",
    "seasonal_blend",
)
INNER_MIN_TRAIN = 28
INNER_MAX_FOLDS = 4
INNER_MIN_FOLDS = 3
MIN_RELATIVE_GAIN = 0.02


@dataclass(frozen=True)
class DemandForecast:
    sku: str
    warehouse_id: str
    daily_rate: float
    volatility: float
    horizon_days: int
    p50_units: int
    p80_units: int
    p95_units: int
    confidence: float
    model_name: str
    observed_days: int
    censored_days: int
    missing_days: int = 0
    model_version: str = MODEL_VERSION
    selection: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def masks(series):
    n = len(series.daily_units)
    missing = series.missing_days if series.missing_days is not None else [False] * n
    stockout = series.stockout_days if series.stockout_days is not None else [False] * n
    if len(missing) != n or len(stockout) != n:
        raise ValueError("missing_days and stockout_days must match daily_units length")
    if any(type(v) is not bool for v in [*missing, *stockout]):
        raise ValueError("demand masks must be boolean")
    if any(
        type(v) not in (int, float) or not isfinite(v) or v < 0
        for v in series.daily_units
    ):
        raise ValueError("daily demand must be finite and non-negative")
    return missing, stockout


def prefix(series, end):
    missing, stockout = masks(series)
    return DemandSeries(
        series.sku,
        series.warehouse_id,
        series.daily_units[:end],
        stockout[:end],
        missing[:end],
    )


def uncensor(series):
    """Use only previous genuine observations; a missing placeholder has no usable value.

    Stockout sales remain a lower bound. Imputations never enter the observation buffer.
    This is a causal heuristic, not identification of unobserved latent demand.
    """
    missing, stockout = masks(series)
    observed = []
    result = []
    for value, m, c in zip(series.daily_units, missing, stockout):
        if m:
            result.append(fmean(observed[-28:]) if observed else 0.0)
        elif c:
            result.append(
                max(float(value), fmean(observed[-28:]) if observed else float(value))
            )
        else:
            observed.append(float(value))
            result.append(float(value))
    return result, len(observed)


def _rate(series, values, horizon, model):
    if model not in CANDIDATES:
        raise ValueError("unknown candidate: " + model)
    if not values:
        return 0.0
    if model.startswith("mean_"):
        return fmean(values[-int(model.split("_")[1]) :])
    if model == "seasonal_naive_7":
        last = values[-7:]
        return fmean(last[i % len(last)] for i in range(horizon))
    if model == "weekday_mean_8":
        if len(values) < 14:
            return fmean(values[-28:])
        return fmean(
            fmean(values[j::7][-8:])
            for j in ((len(values) + d) % 7 for d in range(horizon))
        )
    if model == "ses_02":
        level = fmean(values[:7])
        for v in values[7:]:
            level += 0.2 * (v - level)
        return level
    if model == "damped_trend_28":
        v = values[-28:]
        n = len(v)
        if n < 7:
            return fmean(v)
        center = (n - 1) / 2
        mean = fmean(v)
        slope = sum((i - center) * (y - mean) for i, y in enumerate(v)) / sum(
            (i - center) ** 2 for i in range(n)
        )
        level = mean + slope * center
        expected = level + slope * sum(0.8**d for d in range(1, horizon + 1)) / horizon
        return min(max(0.0, expected), max(1.0, 2 * max(values[-56:])))
    if model == "tsb":
        missing, stockout = masks(series)
        observed = [
            v for v, m, c in zip(series.daily_units, missing, stockout) if not (m or c)
        ]
        first = next((i for i, v in enumerate(observed) if v > 0), None)
        if first is None:
            return 0.0
        size, probability = observed[first], 1 / (first + 1)
        for v in observed[first + 1 :]:
            probability += 0.1 * (float(v > 0) - probability)
            if v > 0:
                size += 0.1 * (v - size)
        return size * probability
    level = 0.6 * fmean(values[-14:]) + 0.4 * fmean(values[-56:])
    return (
        0.7 * level + 0.3 * _rate(series, values, horizon, "weekday_mean_8")
        if len(values) >= 28
        else level
    )


def select_model(series, horizon):
    """Nested rolling validation: every scored test block ends inside this prefix.

    Require at least three valid, disjoint blocks and a 2% error reduction plus
    wins on a majority of blocks. Ties and perfect baselines keep mean28.
    """
    missing, stockout = masks(series)
    n = len(series.daily_units)
    origins = []
    for origin in range(n - horizon, INNER_MIN_TRAIN - 1, -horizon):
        if any(missing[origin : origin + horizon]) or any(
            stockout[origin : origin + horizon]
        ):
            continue
        origins.append(origin)
        if len(origins) == INNER_MAX_FOLDS:
            break
    origins.sort()
    evidence = {
        "inner_origins": origins,
        "inner_horizon": horizon,
        "training_end_exclusive": n,
        "minimum_folds": INNER_MIN_FOLDS,
        "minimum_relative_gain": MIN_RELATIVE_GAIN,
        "fallback": "mean_28",
        "reason": "insufficient_inner_folds",
        "scores": {},
    }
    if len(origins) < INNER_MIN_FOLDS:
        return "mean_28", evidence
    errors = {model: [] for model in CANDIDATES}
    for origin in origins:
        train = prefix(series, origin)
        values, _ = uncensor(train)
        actual = sum(series.daily_units[origin : origin + horizon])
        for model in CANDIDATES:
            errors[model].append(
                abs(_rate(train, values, horizon, model) * horizon - actual)
            )
    scores = {model: fmean(v) for model, v in errors.items()}
    base = scores["mean_28"]
    evidence["scores"] = scores
    evidence["reason"] = "mean_not_clearly_beaten"
    eligible = [
        m
        for m in CANDIDATES
        if scores[m] < base * (1 - MIN_RELATIVE_GAIN) - 1e-9
        and sum(a < b - 1e-9 for a, b in zip(errors[m], errors["mean_28"]))
        > len(origins) / 2
    ]
    if not eligible:
        return "mean_28", evidence
    winner = min(eligible, key=lambda m: (scores[m], CANDIDATES.index(m)))
    evidence.update(
        reason="training_prefix_validation_gain",
        selected_relative_gain=1 - scores[winner] / base,
    )
    return winner, evidence


def forecast(
    series,
    horizon_days=30,
    *,
    cold_start_rate=0.0,
    model="adaptive",
    model_version=MODEL_VERSION,
):
    if type(horizon_days) is not int or not 1 <= horizon_days <= 365:
        raise ValueError("horizon_days must be an integer in [1,365]")
    if (
        type(cold_start_rate) not in (int, float)
        or not isfinite(cold_start_rate)
        or cold_start_rate < 0
    ):
        raise ValueError("invalid cold start rate")
    masks(series)
    if model not in ("adaptive", "legacy_v2", *CANDIDATES):
        raise ValueError("unknown forecast model")
    if model_version not in (MODEL_VERSION, LEGACY_MODEL_VERSION):
        raise ValueError("unsupported forecast model version")
    if model_version == LEGACY_MODEL_VERSION and model not in ("adaptive", "legacy_v2"):
        raise ValueError("fixed candidates require v3")
    if model_version == LEGACY_MODEL_VERSION or model == "legacy_v2":
        old = legacy_forecast(series, horizon_days, cold_start_rate=cold_start_rate)
        return DemandForecast(
            **old.to_dict(),
            model_version=LEGACY_MODEL_VERSION,
            selection={"reason": "archived_v2", "external_prior": cold_start_rate},
        )
    if model_version != MODEL_VERSION:
        raise ValueError("unsupported forecast model version")
    values, observed = uncensor(series)
    # No cross-series category prior without an auditable shared as-of calendar.
    # Low-sample predictions use genuine observed values only, never peer future histories.
    if observed < 7:
        missing, stockout = masks(series)
        seen = [
            float(v)
            for v, m, c in zip(series.daily_units, missing, stockout)
            if not (m or c)
        ]
        rate = fmean(seen) if seen else 0.0
        selected = "observed-cold-start"
        evidence = {
            "reason": "insufficient_observations",
            "training_end_exclusive": len(values),
            "external_prior_used": False,
        }
    else:
        selected, evidence = (
            select_model(series, horizon_days)
            if model == "adaptive"
            else (
                model,
                {"reason": "fixed_comparator", "training_end_exclusive": len(values)},
            )
        )
        rate = _rate(series, values, horizon_days, selected)
    rate = max(0.0, rate)
    volatility = pstdev(values[-56:]) if len(values) > 1 else sqrt(rate)
    confidence = (
        min(0.95, observed / 56)
        * (observed / max(1, len(values)))
        / (1 + volatility / max(rate, 1) * 0.2)
    )
    mean = rate * horizon_days
    sigma = volatility * sqrt(horizon_days)
    return DemandForecast(
        series.sku,
        series.warehouse_id,
        round(rate, 6),
        round(volatility, 6),
        horizon_days,
        ceil(mean),
        ceil(mean + 0.841621 * sigma),
        ceil(mean + 1.644854 * sigma),
        round(confidence, 4),
        selected,
        observed,
        sum(series.stockout_days or []),
        sum(series.missing_days or []),
        MODEL_VERSION,
        evidence,
    )
