"""Deterministic seasonal / TSB baseline; no future observations or current market signals in training.

Quantiles use a normal approximation, not calibrated service guarantees. Censored days are
imputed causally from prior observed demand and never interpreted as true zero demand.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, sqrt
from statistics import fmean, pstdev

from overseas_inventory.models import DemandSeries

MODEL_VERSION = "seasonal-tsb-v2"


@dataclass(frozen=True)
class LegacyDemandForecast:
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

    def to_dict(self) -> dict:
        return asdict(self)


def legacy_uncensor(series: DemandSeries) -> tuple[list[float], int]:
    values = series.daily_units
    flags = series.stockout_days or [False] * len(values)
    missing = series.missing_days or [False] * len(values)
    if len(missing) != len(values):
        raise ValueError("missing_days must match daily_units length")
    if len(flags) != len(values):
        raise ValueError("stockout_days must match daily_units length")
    if any(v < 0 for v in values):
        raise ValueError("daily demand must be non-negative")
    observed: list[float] = []
    result: list[float] = []
    for value, censored in zip(values, [c or m for c, m in zip(flags, missing)]):
        if censored:
            result.append(max(float(value), fmean(observed[-28:]) if observed else float(value)))
        else:
            observed.append(float(value))
            result.append(float(value))
    return result, len(observed)


def legacy_forecast(series: DemandSeries, horizon_days: int = 30, *, cold_start_rate: float = 0.0) -> LegacyDemandForecast:
    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    values, observed = legacy_uncensor(series)
    if not values:
        rate, model = cold_start_rate, "category-cold-start"
    elif observed < 7:
        rate = (sum(values) + cold_start_rate * 7) / (len(values) + 7)
        model = "category-cold-start"
    elif sum(v > 0 for v in values) / len(values) < 0.5:
        # TSB updates occurrence probability on *every* day, decaying obsolete demand.
        first = next((i for i, v in enumerate(values) if v > 0), None)
        size, probability = (values[first], 1 / (first + 1)) if first is not None else (0.0, 0.0)
        for value in values[(first + 1) if first is not None else 0:]:
            probability += 0.1 * (float(value > 0) - probability)
            if value > 0:
                size += 0.1 * (value - size)
        rate, model = size * probability, "TSB"
    else:
        level = 0.6 * fmean(values[-14:]) + 0.4 * fmean(values[-56:])
        if len(values) >= 28:
            # Same weekday positions as the future, with a damped seasonal component.
            seasonal = fmean(fmean(values[j::7][-8:]) for j in ((len(values) + d) % 7 for d in range(horizon_days)))
            rate = 0.7 * level + 0.3 * seasonal
        else:
            rate = level
        model = "damped-seasonal"
    rate = max(0.0, rate)
    volatility = pstdev(values[-56:]) if len(values) > 1 else sqrt(rate)
    confidence = min(0.95, observed / 56) * (observed / max(1, len(values))) / (1 + volatility / max(rate, 1) * 0.2)
    mean = rate * horizon_days
    sigma = volatility * sqrt(horizon_days)
    return LegacyDemandForecast(series.sku, series.warehouse_id, round(rate, 6), round(volatility, 6),
                          horizon_days, ceil(mean), ceil(mean + 0.841621 * sigma),
                          ceil(mean + 1.644854 * sigma), round(confidence, 4), model,
                          observed, sum(series.stockout_days or []), sum(series.missing_days or []))
