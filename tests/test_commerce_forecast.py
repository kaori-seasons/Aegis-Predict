from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from aegis.planning.commerce_backtest import backtest
from aegis.planning.commerce_features import features, known, local_issue
from aegis.planning.commerce_forecast import MODEL, candidate, cold_curve
from overseas_inventory.models import DemandSeries

DAY = date(2026, 1, 1)


def revision(**kw):
    return {
        "id": "r1",
        "revision": 1,
        "published_at": "2025-01-01T00:00:00+00:00",
        "received_at": "2025-01-01T00:00:00+00:00",
        "available_at": "2025-01-01T00:00:00+00:00",
        "is_deleted": False,
        **kw,
    }


def fixture(n=196, signal=True, noise=False):
    context = {
        "config": {
            "commerce_signal_source": "c",
            "commerce_signal_channel": "amazon",
            "commerce_contract_version": "commerce-daily-v1",
            "launch_signal_source": "c",
            "launch_signal_channel": "*",
            "public_signal_source": "p",
        },
        "commerce_performance": [],
        "sku_launches": [],
        "public_signals": [],
        "public_signal_bindings": [],
    }
    units = []
    for i in range(n):
        sessions = 100 + ((i * 37) % 101) * 10
        # A genuinely predictive *lagged* signal, not the contemporaneous target.
        previous = 100 + (((i - 1) * 37) % 101) * 10
        units.append(10 + int(previous / 20) if not noise else 20 + (i % 7) * 3)
        if signal:
            day = DAY + timedelta(days=i)
            clock = local_issue(day + timedelta(days=1), "UTC").isoformat()
            context["commerce_performance"].append(
                revision(
                    id=f"c{i}",
                    source_system="c",
                    sku="S",
                    warehouse_id="W",
                    channel="amazon",
                    contract_version="commerce-daily-v1",
                    metric="sessions",
                    metric_date=str(day),
                    timezone="UTC",
                    period_end_at=clock,
                    published_at=clock,
                    received_at=clock,
                    available_at=clock,
                    state="observed",
                    unit="count",
                    currency="",
                    value=sessions,
                )
            )
    series = DemandSeries("S", "W", units, calendar_start=str(DAY), timezone="UTC")
    return SimpleNamespace(
        commerce_context=context,
        demand_history=[series],
        forecast_as_of=local_issue(DAY + timedelta(days=n), "UTC").isoformat(),
    )


def test_knowledge_filter_precedes_ranking_and_tombstones():
    old = revision(key="x")
    future = revision(
        id="r2",
        key="x",
        revision=2,
        is_deleted=True,
        received_at="2026-03-02T00:00:00+00:00",
    )
    assert known([old, future], "2026-03-01T00:00:00+00:00", ("key",)) == [old]
    assert known([old, future], "2026-03-03T00:00:00+00:00", ("key",)) == []
    with pytest.raises(ValueError):
        known([old], datetime(2026, 3, 1), ("key",))


def test_missing_zero_cvr_and_currency_contracts():
    network = fixture(50)
    day = DAY + timedelta(days=50)
    clock = local_issue(day, "UTC")
    last = network.commerce_context["commerce_performance"][-1]
    last["value"] = 0
    order = revision(
        **{k: v for k, v in last.items() if k not in ("id", "metric", "value")},
        id="order",
        metric="orders",
        value=3,
    )
    network.commerce_context["commerce_performance"].append(order)
    f = features(network.commerce_context, "S", "W", clock, "UTC")
    assert f["values"]["sessions_lag1"] == 0
    assert f["values"]["orders_per_session_lag1"] is None
    last["state"] = "missing"
    last["value"] = None
    f = features(network.commerce_context, "S", "W", clock, "UTC")
    assert (
        f["values"]["sessions_lag1"] is None
        and f["values"]["sessions_lag1_coverage"] == 0
    )
    assert f["values"]["ad_spend_lag28"] is None


@pytest.mark.parametrize(
    "knowledge_clock", ["published_at", "received_at", "available_at"]
)
def test_future_revisions_metrics_and_public_backfills_cannot_change_forecast(
    knowledge_clock,
):
    network = fixture()
    series = network.demand_history[0]
    origin = local_issue(DAY + timedelta(days=168), "UTC")
    before = candidate(network, series, 1, origin)
    polluted = deepcopy(network)
    for r in polluted.commerce_context["commerce_performance"]:
        if date.fromisoformat(r["metric_date"]) >= DAY + timedelta(days=168):
            r["value"] = 99999999
    late = deepcopy(polluted.commerce_context["commerce_performance"][160])
    late.update(id="late", revision=2, value=99999999)
    late[knowledge_clock] = network.forecast_as_of
    polluted.commerce_context["commerce_performance"].append(late)
    polluted.demand_history[0].daily_units[168:] = [999999] * 28
    assert candidate(polluted, polluted.demand_history[0], 1, origin) == before
    assert before[1]["selection"]["reason"] == "purged_inner_validation_passed"
    assert all(
        r["origin"] > before[1]["selection"]["purged_training_end"]
        or r["origin"] == before[1]["selection"]["purged_training_end"]
        for r in before[1]["selection"]["folds"]
    )


def test_no_signal_exact_v3_fallback_and_noise_gate():
    network = fixture(signal=False)
    series = network.demand_history[0]
    from aegis.decision.forecasting import curve

    result, e = candidate(network, series, 7, network.forecast_as_of)
    assert (
        result == curve(series, "rolling-champion-v3", 7, [], network.forecast_as_of)[0]
    )
    assert e["selection"]["reason"] == "no_available_lag_features"
    network = fixture(noise=True)
    assert (
        candidate(network, network.demand_history[0], 7, network.forecast_as_of)[1][
            "selection"
        ]["reason"]
        == "inner_gate_rejected"
    )


def cold_fixture():
    network = fixture(70, signal=False)
    network.demand_history = []
    ctx = network.commerce_context
    for i in range(5):
        code = f"S{i}"
        group = "target" if i == 4 else f"peer{i}"
        launch = DAY + timedelta(days=60 if i == 4 else i * 7)
        series = DemandSeries(
            code,
            "W",
            [
                0
                if DAY + timedelta(days=d) < launch
                else 10 + (DAY + timedelta(days=d) - launch).days
                for d in range(70)
            ],
            calendar_start=str(DAY),
            timezone="UTC",
        )
        network.demand_history.append(series)
        ctx["sku_launches"].append(
            revision(
                id=f"l{i}",
                source_system="c",
                sku=code,
                channel="*",
                launch_group=group,
                cohort_key="accessories",
                event_kind="first_sale",
                event_date=str(launch),
            )
        )
    return network


def test_cold_start_group_holdout_excludes_all_variants_and_warehouses():
    network = cold_fixture()
    target = network.demand_history[-1]
    origin = local_issue(DAY + timedelta(days=61), "UTC")
    before, e = cold_curve(network, target, 7, origin)
    assert before and len(e["donor_groups"]) == 4 and "target" not in e["donor_groups"]
    variant = replace(network.demand_history[0], sku="VAR", daily_units=[999999] * 70)
    network.demand_history.append(variant)
    network.commerce_context["sku_launches"].append(
        revision(
            id="variant",
            source_system="c",
            sku="VAR",
            channel="*",
            launch_group="target",
            cohort_key="accessories",
            event_kind="first_sale",
            event_date=str(DAY),
        )
    )
    assert cold_curve(network, target, 7, origin)[0] == before
    assert candidate(network, target, 7, origin)[1]["execution_blockers"]
    network.commerce_context["sku_launches"] = [
        r
        for r in network.commerce_context["sku_launches"]
        if r.get("launch_group") in ("peer0", "peer1", "target")
    ]
    assert cold_curve(network, target, 7, origin)[0] is None


def test_public_first_seen_bindings_and_no_future_observed_features():
    network = fixture(50)
    context = network.commerce_context
    origin = local_issue(DAY + timedelta(days=50), "UTC")
    day = origin.date()
    binding = revision(
        id="b",
        source_system="p",
        sku="S",
        warehouse_id="W",
        provider="wikimedia",
        series_key="en.wikipedia.org|USB-C",
        metric="pageviews",
        effective_from=str(DAY),
    )
    row = revision(
        id="p",
        source_system="p",
        provider="wikimedia",
        series_key=binding["series_key"],
        metric="pageviews",
        signal_date=str(day - timedelta(days=1)),
        period_end_at=origin.isoformat(),
        signal_kind="observed",
        value=100,
    )
    context["public_signal_bindings"] = [binding]
    context["public_signals"] = [row]
    assert features(context, "S", "W", origin, "UTC")["values"]["pageviews_lag1"] == 100
    row["received_at"] = (origin + timedelta(seconds=1)).isoformat()
    assert (
        features(context, "S", "W", origin, "UTC")["values"]["pageviews_lag1"] is None
    )
    row["received_at"] = "2025-01-01T00:00:00+00:00"
    binding["available_at"] = (origin + timedelta(seconds=1)).isoformat()
    assert (
        features(context, "S", "W", origin, "UTC")["values"]["pageviews_lag1"] is None
    )


def test_paired_reports_and_target_masks():
    network = fixture()
    report = backtest(network, horizon=1, max_folds=3)
    results = report["tracks"]["stock_forward"]["models"]
    assert (
        results[MODEL]["overall"]["count"]
        == results["mean_28"]["overall"]["count"]
        == 3
    )
    assert results[MODEL]["vs_mean28"]["relative_gain"] > 0.5
    assert report["release_eligible"] is False
    network.demand_history[0] = replace(
        network.demand_history[0], missing_days=[False] * 195 + [True]
    )
    assert backtest(network, horizon=1, max_folds=3)["skips"]
    report = backtest(cold_fixture(), horizon=7, max_folds=2, min_train=28)
    assert report["tracks"]["launch_group_holdout"]["independent_launch_groups"] >= 3
    assert all(
        r["launch_group"] not in r["donor_groups"]
        for r in report["rows"]
        if r["track"] == "launch_group_holdout"
    )


def test_changed_public_mapping_invalidates_scope_and_pending_execution():
    import uuid

    from aegis.core.errors import Conflict
    from aegis.decision.forecasting import scope
    from aegis.decision.protection import validate_current_capacity
    from aegis.planning.policy import PlanningPolicy

    config = PlanningPolicy(
        algorithm_version="aegis-protection-v3",
        forecast_model_version=MODEL,
        forecast_result_id=str(uuid.uuid4()),
    )
    binding = revision(
        id="mapping",
        sku="S",
        warehouse_id="W",
        provider="wikimedia",
        series_key="en.wikipedia.org|USB-C",
        metric="pageviews",
        effective_from=str(DAY),
    )
    payload = {
        "stale": False,
        "arrivals": {},
        "policy": config.model_dump(),
        "network": {
            "demand_history": [{"sku": "S", "warehouse_id": "W", "timezone": "UTC"}],
            "commerce_context": {
                "config": {"public_signal_source": "p"},
                "public_signal_bindings": [binding],
            },
        },
    }
    original = scope(payload, [7])
    payload["network"]["commerce_context"]["public_signal_bindings"][0][
        "series_key"
    ] = "en.wikipedia.org|Laptop"
    assert scope(payload, [7]) != original
    with pytest.raises(Conflict, match="mapping contract changed"):
        validate_current_capacity(
            config,
            {"arrivals": {}, "prediction": {"scope": original, "horizons": [7]}},
            payload,
            [],
        )
