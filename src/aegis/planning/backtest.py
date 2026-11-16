"""Paired rolling-origin comparison with nested training-only selection.

These are retrospective tests on frozen histories, not bitemporal source reconstruction.
No cross-series priors are used. ABC is withheld unless alignment is explicitly known.
"""

from __future__ import annotations
from collections import Counter, defaultdict
from statistics import fmean
from overseas_inventory.models import Network
from ..core.util import content_hash
from .forecast import (
    CANDIDATES,
    MODEL_VERSION,
    LEGACY_MODEL_VERSION,
    forecast,
    masks,
    prefix,
    uncensor,
)

COMPARATORS = ("adaptive", *CANDIDATES, "legacy_v2")
EVALUATION_VERSION = "paired-rolling-v3"


def _metrics(rows):
    n = len(rows)
    actual = sum(r["actual"] for r in rows)
    absolute = sum(abs(r["prediction"] - r["actual"]) for r in rows)
    signed = sum(r["prediction"] - r["actual"] for r in rows)

    def loss(q, key):
        return (
            fmean(
                max(q * (r["actual"] - r[key]), (q - 1) * (r["actual"] - r[key]))
                for r in rows
            )
            if n
            else None
        )

    return {
        "wape": absolute / actual if actual else None,
        "bias": signed / actual if actual else None,
        "mae": absolute / n if n else None,
        "mean_error": signed / n if n else None,
        "absolute_error": absolute,
        "actual_units": actual,
        "folds": n,
        "zero_actual_folds": sum(r["actual"] == 0 for r in rows),
        "p80_coverage": sum(r["actual"] <= r["p80"] for r in rows) / n if n else None,
        "p95_coverage": sum(r["actual"] <= r["p95"] for r in rows) / n if n else None,
        "p50_pinball": loss(0.5, "p50"),
        "p80_pinball": loss(0.8, "p80"),
        "p95_pinball": loss(0.95, "p95"),
    }


def _paired(rows, base):
    if len(rows) != len(base) or any(
        r["actual"] != b["actual"] for r, b in zip(rows, base)
    ):
        raise ValueError("model comparison requires identical paired holdouts")
    error = sum(abs(r["prediction"] - r["actual"]) for r in rows)
    base_error = sum(abs(r["prediction"] - r["actual"]) for r in base)
    wins = sum(
        abs(r["prediction"] - r["actual"]) < abs(b["prediction"] - b["actual"]) - 1e-5
        for r, b in zip(rows, base)
    )
    losses = sum(
        abs(r["prediction"] - r["actual"]) > abs(b["prediction"] - b["actual"]) + 1e-5
        for r, b in zip(rows, base)
    )
    return {
        "skill": 1 - error / base_error if base_error > 1e-9 else None,
        "absolute_error_difference": error - base_error,
        "wins": wins,
        "losses": losses,
        "ties": len(rows) - wins - losses,
        "paired_folds": len(rows),
        "baseline_error": base_error,
        "status": "no_folds"
        if not rows
        else "baseline_perfect"
        if base_error <= 1e-9
        else "compared",
    }


def rolling_backtest(
    network: Network,
    *,
    horizon=7,
    min_train=28,
    max_folds_per_series=8,
    model_version=MODEL_VERSION,
    calendar_aligned=False,
    include_folds=True,
):
    if (
        type(horizon) is not int
        or not 1 <= horizon <= 365
        or type(min_train) is not int
        or min_train < 7
    ):
        raise ValueError("invalid backtest windows")
    if max_folds_per_series is not None and (
        type(max_folds_per_series) is not int or max_folds_per_series < 1
    ):
        raise ValueError("max_folds_per_series must be positive or None")
    if model_version not in (MODEL_VERSION, LEGACY_MODEL_VERSION):
        raise ValueError("unsupported evaluation model version")
    histories = list(network.demand_history)
    keys = [(s.sku, s.warehouse_id) for s in histories]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate forecast series")
    if calendar_aligned and len({len(s.daily_units) for s in histories}) > 1:
        raise ValueError("ABC alignment requires equal calendar lengths")
    abc = {s.sku: "unavailable" for s in histories}
    if calendar_aligned:
        volume = defaultdict(float)
        for s in histories:
            missing, stockout = masks(s)
            volume[s.sku] += sum(
                v
                for v, m, c in zip(
                    s.daily_units[:min_train], missing[:min_train], stockout[:min_train]
                )
                if not (m or c)
            )
        total = sum(volume.values())
        cumulative = 0
        for sku in sorted(volume, key=lambda k: (-volume[k], k)):
            abc[sku] = (
                "A"
                if cumulative < total * 0.8
                else "B"
                if cumulative < total * 0.95
                else "C"
            )
            cumulative += volume[sku]
    grouped = {m: defaultdict(list) for m in COMPARATORS}
    skipped = Counter()
    selection = Counter()
    folds = []
    retained = []
    for s in sorted(histories, key=lambda s: (s.sku, s.warehouse_id)):
        missing, stockout = masks(s)
        origins = list(range(min_train, len(s.daily_units) - horizon + 1, horizon))
        if max_folds_per_series:
            origins = origins[-max_folds_per_series:]
        for origin in origins:
            if any(missing[origin : origin + horizon]) or any(
                stockout[origin : origin + horizon]
            ):
                skipped["unobserved_test_window"] += 1
                continue
            train = prefix(s, origin)
            if uncensor(train)[1] < 7:
                skipped["insufficient_training_observations"] += 1
                continue
            # Every prediction is made before the held-out quantities are scored.
            predictions = {m: forecast(train, horizon, model=m) for m in COMPARATORS}
            actual = sum(s.daily_units[origin : origin + horizon])
            group_keys = (
                "overall",
                f"category:{network.skus[s.sku].category}",
                f"warehouse:{s.warehouse_id}",
                f"abc:{abc[s.sku]}",
                f"series:{s.sku}/{s.warehouse_id}",
            )
            row = {
                "sku": s.sku,
                "warehouse_id": s.warehouse_id,
                "origin": origin,
                "train_end_exclusive": origin,
                "test_end_exclusive": origin + horizon,
                "actual": actual,
                "train_digest": content_hash(
                    {
                        "units": train.daily_units,
                        "missing": train.missing_days,
                        "stockout": train.stockout_days,
                    }
                ),
                "predictions": {},
                "selection": predictions["adaptive"].selection,
            }
            for m, p in predictions.items():
                item = {
                    "prediction": p.daily_rate * horizon,
                    "actual": actual,
                    "p50": p.p50_units,
                    "p80": p.p80_units,
                    "p95": p.p95_units,
                }
                for group in group_keys:
                    grouped[m][group].append(item)
                row["predictions"][m] = {**item, "selected_model": p.model_name}
            selection[predictions["adaptive"].model_name] += 1
            primary = (
                "legacy_v2" if model_version == LEGACY_MODEL_VERSION else "adaptive"
            )
            row["prediction"] = row["predictions"][primary]["prediction"]
            folds.append(row)
            retained.append((s.sku, s.warehouse_id, origin))
    models = {}
    for m in COMPARATORS:
        groups = {key: _metrics(rows) for key, rows in grouped[m].items()}
        overall = groups.get("overall", _metrics([]))
        pairs = {
            key: _paired(rows, grouped["mean_28"][key])
            for key, rows in grouped[m].items()
        }
        models[m] = {
            "overall": overall,
            "by_group": groups,
            "vs_baseline": pairs.get("overall", _paired([], [])),
            "paired_by_group": pairs,
        }
    primary = "legacy_v2" if model_version == LEGACY_MODEL_VERSION else "adaptive"
    metrics = dict(models[primary]["by_group"])
    metrics["overall"] = models[primary]["overall"]
    metrics["baseline"] = models["mean_28"]["overall"]
    return {
        "evaluation_version": EVALUATION_VERSION,
        "model_version": model_version,
        "primary_model": primary,
        "horizon": horizon,
        "min_train": min_train,
        "max_folds_per_series": max_folds_per_series,
        "metrics": metrics,
        "models": models,
        "selection_counts": dict(selection),
        "skipped": dict(skipped),
        "fold_set_digest": content_hash(retained),
        "evaluated_series": len({(a, b) for a, b, _ in retained}),
        "abc_mode": "aligned_initial_training_prefix"
        if calendar_aligned
        else "withheld_no_shared_calendar",
        "point_metric": "expected units = daily_rate * horizon, without integer rounding",
        "leakage_scope": "prefix-only processing and nested selection on frozen histories; no source revision/as-of reconstruction",
        "folds": folds if include_folds else [],
    }
