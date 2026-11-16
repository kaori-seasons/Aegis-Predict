"""Rolling origins with training-only cold starts and non-overlapping holdouts."""
from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from overseas_inventory.models import DemandSeries, Network
from .forecast import forecast, uncensor


def rolling_backtest(network: Network, *, horizon: int = 7, min_train: int = 28) -> dict:
    if horizon < 1 or min_train < 7:
        raise ValueError("invalid backtest windows")
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    folds = []
    # ABC classification uses the initial training window only.
    volume = {s.sku: 0 for s in network.demand_history}
    for s in network.demand_history:
        volume[s.sku] += sum(s.daily_units[:min_train])
    total = sum(volume.values())
    abc, cumulative = {}, 0
    for sku in sorted(volume, key=lambda k: (-volume[k], k)):
        abc[sku] = "A" if cumulative < total * 0.8 else "B" if cumulative < total * 0.95 else "C"
        cumulative += volume[sku]
    for series in network.demand_history:
        for origin in range(min_train, len(series.daily_units) - horizon + 1, horizon):
            flags = series.stockout_days or [False] * len(series.daily_units)
            missing = series.missing_days or [False] * len(series.daily_units)
            if any(flags[origin:origin + horizon]) or any(missing[origin:origin + horizon]):
                continue  # Censored test sales are not observed demand.
            train = DemandSeries(series.sku, series.warehouse_id, series.daily_units[:origin], flags[:origin], missing[:origin])
            prediction = forecast(train, horizon).daily_rate * horizon
            actual = sum(series.daily_units[origin:origin + horizon])
            pair = (prediction, actual)
            category = network.skus[series.sku].category
            for key in ("overall", f"category:{category}", f"abc:{abc[series.sku]}", f"warehouse:{series.warehouse_id}"):
                buckets[key].append(pair)
            baseline = fmean(uncensor(train)[0][-28:]) * horizon
            buckets["baseline"].append((baseline, actual))
            folds.append({"sku": series.sku, "warehouse_id": series.warehouse_id, "origin": origin,
                          "prediction": prediction, "actual": actual})
    metrics = {}
    for key, pairs in sorted(buckets.items()):
        denom = sum(a for _, a in pairs)
        metrics[key] = {"wape": sum(abs(p - a) for p, a in pairs) / denom if denom else None,
                        "bias": sum(p - a for p, a in pairs) / denom if denom else None,
                        "actual_units": denom, "folds": len(pairs)}
    return {"horizon": horizon, "min_train": min_train, "metrics": metrics, "folds": folds}
