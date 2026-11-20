"""Paired stock rolling origins + launch-entity group holdouts; research, never release evidence."""

from __future__ import annotations

from datetime import date, timedelta
from math import sqrt

from ..decision.forecasting import curve, metrics
from .commerce_features import launch_events, local_issue
from .commerce_forecast import MODEL, candidate, lifecycle, prefix
from .forecast import masks


def backtest(network, *, horizon=7, max_folds=4, min_train=112):
    if (
        not 1 <= horizon <= 28
        or not 1 <= max_folds <= 12
        or min_train < 28
        or len(network.demand_history) > 30
    ):
        raise ValueError(
            "commerce backtest budget: horizon1–28, folds1–12, series<=30, min_train>=28"
        )
    from ..core.util import content_hash, json_safe

    rows = []
    skips = []
    for series in sorted(network.demand_history, key=lambda s: (s.sku, s.warehouse_id)):
        if not series.calendar_start:
            raise ValueError("explicit calendar_start is required")
        start = date.fromisoformat(series.calendar_start)
        n = len(series.daily_units)
        missing, censored = masks(series)
        origins = [
            ("stock_forward", i) for i in range(min_train, n - horizon + 1, horizon)
        ][-max_folds:]
        # The launch group is held out across ALL SKUs/warehouses. The prior is trained on
        # other mature groups only. Own history shrinkage is explicitly labelled.
        events = launch_events(network.commerce_context, network.forecast_as_of)
        first = next(
            (
                r
                for r in events
                if r["sku"] == series.sku and r["event_kind"] == "first_sale"
            ),
            None,
        )
        if first:
            index = (date.fromisoformat(str(first["event_date"])) - start).days
            origins += [("launch_group_holdout", index + age) for age in (1, 7, 14, 21)]
        for track, i in origins:
            if i < 0 or i + horizon > n:
                continue
            day = start + timedelta(days=i)
            origin = local_issue(day, series.timezone)
            if network.forecast_as_of and origin > __import__(
                "datetime"
            ).datetime.fromisoformat(network.forecast_as_of):
                continue
            if any(missing[j] or censored[j] for j in range(i, i + horizon)):
                skips.append(
                    {
                        "sku": series.sku,
                        "warehouse_id": series.warehouse_id,
                        "track": track,
                        "origin": str(day),
                        "reason": "incomplete_or_censored_actual",
                    }
                )
                continue
            past = prefix(series, day)
            life = lifecycle(network.commerce_context, past, origin)
            if (
                track == "stock_forward"
                and life["event"]
                and life["stage"] != "established"
            ):
                continue
            if track == "launch_group_holdout" and (
                not life["event"] or life["event"]["event_kind"] != "first_sale"
            ):
                skips.append(
                    {
                        "sku": series.sku,
                        "track": track,
                        "origin": str(day),
                        "reason": "launch_not_yet_known",
                    }
                )
                continue
            group = life["event"]["launch_group"] if life["event"] else None
            actual = sum(series.daily_units[i : i + horizon])
            pair_id = content_hash(
                [track, series.sku, series.warehouse_id, str(day), horizon]
            )
            for model in ("mean_28", "rolling-champion-v3", MODEL):
                daily, evidence = (
                    candidate(
                        network,
                        past,
                        horizon,
                        origin,
                        heldout_groups=(group,) if group else (),
                    )
                    if model == MODEL
                    else curve(past, model, horizon, [], origin.isoformat())
                )
                total = sum(daily)
                rows.append(
                    {
                        "pair_id": pair_id,
                        "sku": series.sku,
                        "warehouse_id": series.warehouse_id,
                        "track": track,
                        "launch_group": group,
                        "origin": str(day),
                        "horizon": horizon,
                        "model": model,
                        "actual": actual,
                        "expected": total,
                        "p95": total
                        + 1.644854 * evidence.get("volatility", 0) * sqrt(horizon),
                        "calibrated": False,
                        "selection": evidence.get("selection", {}),
                        "donor_groups": evidence.get("lifecycle", {}).get(
                            "donor_groups", []
                        ),
                        "feature_digest": evidence.get("features", {}).get("digest"),
                    }
                )
    tracks = {}
    for track in ("stock_forward", "launch_group_holdout"):
        selected = [r for r in rows if r["track"] == track]
        baselines = {r["pair_id"]: r for r in selected if r["model"] == "mean_28"}
        models = {}
        for model in ("mean_28", "rolling-champion-v3", MODEL):
            paired = [r for r in selected if r["model"] == model]
            base_error = sum(
                abs(baselines[r["pair_id"]]["expected"] - r["actual"]) for r in paired
            )
            error = sum(abs(r["expected"] - r["actual"]) for r in paired)
            models[model] = {
                "overall": metrics(paired),
                "vs_mean28": {
                    "baseline_error": base_error,
                    "absolute_error_difference": error - base_error,
                    "relative_gain": 1 - error / base_error if base_error else None,
                },
                "selected_feature_folds": sum(
                    r["selection"].get("reason") == "purged_inner_validation_passed"
                    for r in paired
                ),
                "selected_cold_folds": sum(
                    r["selection"].get("reason") == "cold_start_group_prior"
                    for r in paired
                ),
            }
        tracks[track] = {
            "models": models,
            "independent_launch_groups": len(
                {r["launch_group"] for r in selected if r["launch_group"]}
            ),
        }
    return json_safe(
        {
            "schema": "commerce-backtest-v1",
            "model": MODEL,
            "horizon": horizon,
            "tracks": tracks,
            "rows": rows,
            "skips": skips,
            "input_digest": content_hash(
                {
                    "demand": [vars(s) for s in network.demand_history],
                    "context": network.commerce_context,
                    "as_of": network.forecast_as_of,
                }
            ),
            "release_eligible": False,
            "knowledge_semantics": "features use published/received/available <= each issue before revision selection; targets never enter prediction",
            "limitations": [
                "Frozen demand snapshots may contain later demand corrections; retrospective results are research only.",
                "Latest public API history is first-seen, never backdated. Real release evidence requires forward-issued forecasts and mature scored actuals.",
                "Launch-group holdout excludes the entity from donors across variants/warehouses; own observed history is explicitly shrunk.",
            ],
        }
    )
