from copy import deepcopy
from dataclasses import replace
from math import inf, nan
from types import SimpleNamespace
import pytest
from overseas_inventory.models import DemandSeries
from aegis.planning.forecast import (
    forecast,
    uncensor,
    MODEL_VERSION,
    LEGACY_MODEL_VERSION,
    CANDIDATES,
    prefix,
)
from aegis.planning.forecast_legacy import legacy_forecast
from aegis.planning.backtest import rolling_backtest


def net(*series):
    return SimpleNamespace(
        demand_history=list(series),
        skus={s.sku: SimpleNamespace(category="c") for s in series},
    )


def trend(n=168):
    return DemandSeries("S", "W", [5 + i // 4 + (i % 7) for i in range(n)])


def test_outer_targets_and_future_tail_cannot_change_prediction_or_selection():
    s = trend()
    original = rolling_backtest(net(s), max_folds_per_series=None)
    changed = deepcopy(s)
    changed.daily_units[84:] = [100000 + i for i in range(84)]
    poisoned = rolling_backtest(net(changed), max_folds_per_series=None)
    # Fold at 84 sees *all* its test outcomes and all later history replaced.
    a = next(r for r in original["folds"] if r["origin"] == 84)
    b = next(r for r in poisoned["folds"] if r["origin"] == 84)
    assert a["actual"] != b["actual"]
    assert a["predictions"] == {
        m: {**v, "actual": a["actual"]} for m, v in b["predictions"].items()
    }
    assert a["selection"] == b["selection"] and a["train_digest"] == b["train_digest"]


def test_nested_validation_is_inside_each_outer_training_prefix():
    report = rolling_backtest(net(trend()))
    assert report["selection_counts"]
    for row in report["folds"]:
        assert row["train_end_exclusive"] == row["origin"]
        assert row["test_end_exclusive"] == row["origin"] + 7
        selection = row["selection"]
        origins = selection["inner_origins"]
        assert all(o + selection["inner_horizon"] <= row["origin"] for o in origins)
        assert all(
            b - a >= selection["inner_horizon"] for a, b in zip(origins, origins[1:])
        )
        assert selection["training_end_exclusive"] == row["origin"]


def test_future_of_other_series_and_market_data_cannot_change_target():
    s = trend(140)
    peer = DemandSeries("T", "W", [100] * 200)
    source = net(s, peer)
    source.market_signals = {"future-price": 999999}
    a = rolling_backtest(source)
    peer2 = replace(peer, daily_units=[1000000] * 200)
    other = net(s, peer2)
    other.market_signals = {"future-price": 0}
    b = rolling_backtest(other)
    left = [r for r in a["folds"] if r["sku"] == "S"]
    right = [r for r in b["folds"] if r["sku"] == "S"]
    assert left == right
    market_only = net(s, peer)
    market_only.market_signals = {"future-price": -10000}
    assert rolling_backtest(market_only)["folds"] == a["folds"]
    assert (
        forecast(prefix(s, 80), cold_start_rate=1).to_dict()
        == forecast(prefix(s, 80), cold_start_rate=999999).to_dict()
    )


def test_missing_placeholder_is_not_observed_and_preprocessing_is_causal():
    s = DemandSeries(
        "S",
        "W",
        [10] * 50 + [0] + [12] * 49,
        missing_days=[False] * 50 + [True] + [False] * 49,
    )
    poison = deepcopy(s)
    poison.daily_units[50] = 999999
    assert uncensor(s) == uncensor(poison)
    assert forecast(s).to_dict() == forecast(poison).to_dict()
    assert uncensor(
        DemandSeries("S", "W", [0, 10, 0, 999], missing_days=[True, False, True, False])
    )[0][:3] == [0, 10, 10]
    assert (
        uncensor(
            DemandSeries("S", "W", [10, 30, 100], stockout_days=[False, True, False])
        )[0][1]
        == 30
    )


def test_insufficient_inner_evidence_and_perfect_baseline_keep_mean28():
    small = forecast(DemandSeries("S", "W", [9] * 35), 7)
    assert (
        small.model_name == "mean_28"
        and small.selection["reason"] == "insufficient_inner_folds"
    )
    stable = forecast(DemandSeries("S", "W", [9] * 100), 7)
    assert (
        stable.model_name == "mean_28"
        and stable.selection["reason"] == "mean_not_clearly_beaten"
    )


def test_tsb_does_not_treat_missing_days_as_no_demand_events():
    observed = DemandSeries("S", "W", [0, 0, 20] * 20)
    gap = DemandSeries(
        "S",
        "W",
        observed.daily_units + [0] * 30,
        missing_days=[False] * 60 + [True] * 30,
    )
    assert (
        forecast(observed, 7, model="tsb").daily_rate
        == forecast(gap, 7, model="tsb").daily_rate
    )
    zero = replace(gap, missing_days=[False] * 90)
    assert (
        forecast(zero, 7, model="tsb").daily_rate
        < forecast(gap, 7, model="tsb").daily_rate * 0.1
    )


def test_comparisons_share_fold_set_and_count_zeros_without_undefined_skill_claims():
    s = DemandSeries("S", "W", [0] * 70)
    r = rolling_backtest(net(s))
    n = r["metrics"]["overall"]["folds"]
    assert n > 0 and r["metrics"]["overall"]["mae"] == 0
    for m, v in r["models"].items():
        assert v["overall"]["folds"] == n and v["overall"]["wape"] is None
        assert (
            v["vs_baseline"]["skill"] is None
            and v["vs_baseline"]["status"] == "baseline_perfect"
        )
        assert v["overall"]["p80_coverage"] == 1
    assert (
        len({tuple((row["origin"], row["test_end_exclusive"])) for row in r["folds"]})
        == n
    )


def test_unobserved_outer_windows_excluded_from_every_candidate():
    s = DemandSeries(
        "S", "W", [10] * 100, stockout_days=[False] * 35 + [True] * 7 + [False] * 58
    )
    r = rolling_backtest(net(s), max_folds_per_series=None)
    assert 35 not in [f["origin"] for f in r["folds"]]
    assert r["skipped"]["unobserved_test_window"] == 1
    assert all(
        v["overall"]["folds"] == r["metrics"]["overall"]["folds"]
        for v in r["models"].values()
    )


def test_v2_numeric_reproduction_is_retained():
    s = DemandSeries(
        "S",
        "W",
        [10] * 28 + [0] * 7 + [12] * 35,
        missing_days=[False] * 28 + [True] * 7 + [False] * 35,
    )
    old = legacy_forecast(s, 14, cold_start_rate=6).to_dict()
    archived = forecast(s, 14, cold_start_rate=6, model_version=LEGACY_MODEL_VERSION)
    assert {k: archived.to_dict()[k] for k in old} == old
    assert archived.model_version == LEGACY_MODEL_VERSION
    with pytest.raises(ValueError, match="unsupported"):
        forecast(s, model_version="unknown")
    with pytest.raises(ValueError, match="unsupported"):
        forecast(s, model="legacy_v2", model_version="unknown")


@pytest.mark.parametrize("bad", [nan, inf, -1, True])
def test_bad_values_fail_closed(bad):
    with pytest.raises(ValueError):
        forecast(DemandSeries("S", "W", [bad] * 40))


def test_invalid_masks_and_empty_series():
    with pytest.raises(ValueError):
        forecast(DemandSeries("S", "W", [1] * 30, missing_days=[False]))
    with pytest.raises(ValueError):
        forecast(DemandSeries("S", "W", [1] * 30, stockout_days=[1] * 30))
    assert forecast(DemandSeries("S", "W", [])).p95_units == 0
    assert (
        forecast(DemandSeries("S", "W", [3, 4]), cold_start_rate=100000).daily_rate
        == 3.5
    )
    with pytest.raises(ValueError):
        forecast(DemandSeries("S", "W", []), model="unknown")


def test_quantile_diagnostics_and_paired_metrics_are_recomputable():
    r = rolling_backtest(net(trend()), max_folds_per_series=4)
    for m, v in r["models"].items():
        rows = [f["predictions"][m] for f in r["folds"]]
        expected = sum(abs(x["prediction"] - x["actual"]) for x in rows) / sum(
            x["actual"] for x in rows
        )
        assert v["overall"]["wape"] == pytest.approx(expected)
        assert v["overall"]["p95_pinball"] >= 0
        assert 0 <= v["overall"]["p80_coverage"] <= 1
    assert any(row["selection"]["scores"] for row in r["folds"])


def test_point_model_improvement_is_not_assumed_at_every_horizon():
    # Prespecified deterministic trend fixture checks relative-error computation,
    # while deployment decisions are not inferred from a single small example.
    result = rolling_backtest(net(trend(210)), horizon=7)
    pair = result["models"]["adaptive"]["vs_baseline"]
    assert (
        pair["skill"] > 0
        and pair["paired_folds"] == result["models"]["mean_28"]["overall"]["folds"]
    )
    from aegis.planning.policy import PlanningPolicy

    assert PlanningPolicy().forecast_model_version == LEGACY_MODEL_VERSION
    assert (
        PlanningPolicy(forecast_model_version=MODEL_VERSION).forecast_model_version
        == MODEL_VERSION
    )


def test_cli_baseline_gate_rejects_known_regression(tmp_path, monkeypatch, capsys):
    import sys
    from aegis import cli

    fake = {
        "metrics": {"overall": {"wape": 0.2, "bias": 0.1, "folds": 3}},
        "model_version": MODEL_VERSION,
        "horizon": 30,
        "primary_model": "adaptive",
        "models": {
            "adaptive": {
                "overall": {"wape": 0.2},
                "vs_baseline": {
                    "absolute_error_difference": 2.0,
                    "baseline_error": 10.0,
                },
            }
        },
    }
    monkeypatch.setattr(
        "aegis.planning.backtest.rolling_backtest", lambda *a, **kw: fake
    )
    with pytest.raises(SystemExit, match="paired mean28 regression"):
        cli.main(["backtest", "--max-baseline-regression", "0"])
    capsys.readouterr()


def test_archived_v2_golden_numbers_from_the_original_documented_example():
    values = [(8, 10, 11, 9, 13, 17, 15)[i % 7] for i in range(70)]
    for i in (18, 39, 62, 10, 27, 43, 65):
        values[i] = 0
    values[30] = 2
    values[54] = 1
    s = DemandSeries(
        "S",
        "W",
        values,
        [i in (30, 54) for i in range(70)],
        [i in (18, 39, 62) for i in range(70)],
    )
    fc = forecast(s, 14, model_version=LEGACY_MODEL_VERSION)
    assert (
        fc.daily_rate,
        fc.volatility,
        fc.p50_units,
        fc.p80_units,
        fc.p95_units,
        fc.confidence,
    ) == (10.904039, 3.88736, 153, 165, 177, 0.8234)
