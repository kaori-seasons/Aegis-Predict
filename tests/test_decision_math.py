"""Deterministic edge cases independent of database clocks or synthetic scores."""

from datetime import date, timedelta

import pytest

from aegis.core.errors import InvalidInput
from aegis.decision.collaboration import allocate_total
from aegis.decision.evaluation import empirical_quantile, enough, independent
from aegis.decision.forecasting import curve, metrics
from overseas_inventory.models import DemandSeries


def test_curve_fixed_selection_cumulative_and_calendar():
    s = DemandSeries(
        "S", "W", [1, 2, 3, 4, 5, 6, 100] * 30, calendar_start="2026-01-01"
    )
    daily, ev = curve(s, "rolling-champion-v3", 30, [], "2026-08-01T00:00:00Z")
    assert len(daily) == 30 and min(daily) >= 0
    assert ev["model_name"] == "seasonal_naive_7"
    assert sum(daily[:7]) == pytest.approx(121)
    # A 30-day average projected back to7 would incorrectly flatten weekdays.
    assert sum(daily[:7]) != pytest.approx(sum(daily) / 30 * 7)


def test_integer_and_pack_allocations_conserve_total_and_fail_closed():
    rows = [
        {"sku": "A", "warehouse_id": "W", "weight": 1, "pack_size": 5},
        {"sku": "B", "warehouse_id": "W", "weight": 1, "pack_size": 7},
    ]
    assert sum(r["units"] for r in allocate_total(13, rows)) == 13
    packs = allocate_total(12, rows, whole_packs=True)
    assert [r["units"] for r in packs] == [5, 7]
    with pytest.raises(InvalidInput):
        allocate_total(1, rows, whole_packs=True)
    with pytest.raises(InvalidInput):
        allocate_total(10, [{**r, "weight": 0} for r in rows])


def test_nonoverlapping_evidence_cannot_be_inflated_by_repeated_issues():
    rows = []
    for i in range(30):
        day = date(2026, 1, 1) + timedelta(days=i)
        rows.append(
            {
                "sku": "S",
                "warehouse_id": "W",
                "model": "M",
                "horizon": 7,
                "target_start": str(day),
                "target_end": str(day + timedelta(days=6)),
                "origin": str(day),
            }
        )
    assert len(independent(rows * 10)) == 5
    assert not enough(rows * 1000)
    assert empirical_quantile([1, 2, 3, 4], 0.95) == 4
    assert metrics([{"actual": 0, "expected": 0, "p95": 0}])["wape"] is None


def test_market_daily_curve_does_not_extrapolate_first_week_campaign():
    from aegis.planning.market_forecast import FEATURES

    n = 180
    first = date(2026, 1, 1)
    origin = first + timedelta(days=n)
    historical = [3 if (i // 5) % 2 else 1 for i in range(n)]
    future = [3] * 7 + [1] * 23
    s = DemandSeries("S", "W", [10 * v for v in historical], calendar_start=str(first))
    revisions = [
        dict(
            sku="S",
            warehouse_id="W",
            source_system="mock:test",
            platform="amazon",
            signal_kind="planned",
            signal_date=str(first + timedelta(days=i)),
            revision=1,
            published_at="2025-12-01T00:00:00Z",
            received_at="2025-12-01T00:00:00Z",
            available_at="2025-12-01T00:00:00Z",
            **{k: (lift if k == "promotion_lift" else 1.0) for k in FEATURES},
        )
        for i, lift in enumerate(historical + future)
    ]
    daily, evidence = curve(
        s, "market-aware-v4", 30, revisions, str(origin) + "T00:00:00Z"
    )
    assert evidence["selection"]["market_enabled"]
    assert sum(daily[:7]) / 7 > sum(daily[7:]) / 23 * 1.8
    poisoned = [
        {
            **r,
            "promotion_lift": 1000,
            "revision": 2,
            "published_at": "2027-01-01T00:00:00Z",
            "available_at": "2027-01-01T00:00:00Z",
        }
        for r in revisions
    ]
    assert (
        curve(
            s, "market-aware-v4", 30, revisions + poisoned, str(origin) + "T00:00:00Z"
        )[0]
        == daily
    )


def test_promotion_positive_control_and_mock_failure(monkeypatch):
    import uuid
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aegis.decision import evaluation, history

    calid = uuid.uuid4()
    frozenid = uuid.uuid4()
    inputid = uuid.uuid4()
    cal = SimpleNamespace(
        id=calid,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        payload={
            "scope": "scope",
            "model": "rolling-champion-v3",
            "code_digest": "code",
            "ready": True,
            "support": {"7": {}},
            "max_training_target_end": "2024-12-01",
        },
    )
    samples = []
    for i in range(20):
        day = date(2025, 2, 1) + timedelta(days=i * 7)
        for j in range(30):
            actual = 11 if i == 0 else 10
            for model, expected in [
                ("rolling-champion-v3", 10),
                ("mean_28", 12),
                ("seasonal-tsb-v2", 13),
            ]:
                samples.append(
                    {
                        "forecast_id": str(frozenid),
                        "sku": str(j),
                        "warehouse_id": "W",
                        "horizon": 7,
                        "target_start": str(day),
                        "target_end": str(day + timedelta(days=6)),
                        "origin": str(day) + "T00:00:00+00:00",
                        "model": model,
                        "actual": actual,
                        "expected": expected,
                        "p95": 10,
                        "calibrated": True,
                        "calibration_id": str(calid),
                        "code_digest": "code",
                        "evaluation_id": "evaluation",
                    }
                )
    monkeypatch.setattr(evaluation, "all_samples", lambda *_: samples)
    monkeypatch.setattr(
        evaluation,
        "job_reliability",
        lambda *_: {"completed": 20, "failed": 0, "failure_rate": 0.0},
    )
    cp = SimpleNamespace()
    fc = SimpleNamespace(payload={"input_id": str(inputid)})
    inp = SimpleNamespace(
        checkpoint_id=uuid.uuid4(),
        payload={
            "stale": False,
            "watermarks": {"authorities": [{"connection_id": "c"}]},
        },
    )
    monkeypatch.setattr(
        evaluation, "verified", lambda _, ident, *args: fc if ident == frozenid else inp
    )
    monkeypatch.setattr(history, "require_checkpoint", lambda *_: cp)
    monkeypatch.setattr(
        history, "facts", lambda *_: [({"id": "c", "is_mock": False}, 1)]
    )
    report = evaluation.promotion_report(None, cal, "seasonal-tsb-v2")
    assert report["eligible"], report
    assert report["horizons"]["7"]["support"] == {
        "samples": 600,
        "origins": 20,
        "series": 30,
    }
    monkeypatch.setattr(
        history, "facts", lambda *_: [({"id": "c", "is_mock": True}, 1)]
    )
    assert (
        "mock_evidence_ineligible_for_execution"
        in evaluation.promotion_report(None, cal, "seasonal-tsb-v2")["reasons"]
    )
    monkeypatch.setattr(
        evaluation,
        "job_reliability",
        lambda *_: {"completed": 20, "failed": 1, "failure_rate": 0.05},
    )
    assert (
        "runtime_reliability_evidence_insufficient"
        in evaluation.promotion_report(None, cal, "seasonal-tsb-v2")["reasons"]
    )


def test_quantile_horizon_envelope_records_raw_and_scores_correction():
    from aegis.decision.forecasting import monotone_intervals

    intervals = {
        "7": {"p50": 50, "p80": 80, "p95": 100},
        "17": {"p50": 60, "p80": 70, "p95": 90},
    }
    monotone_intervals(intervals)
    assert intervals["17"]["raw_quantiles"] == {"p50": 60, "p80": 70, "p95": 90}
    assert intervals["17"]["p95"] == 100 and intervals["17"]["p80"] == 80
    assert intervals["17"]["horizon_envelope_adjusted"]


def test_route_specific_protection_and_arrival_timing():
    import uuid

    from aegis.decision.protection import solve_protection
    from aegis.planning.policy import PlanningPolicy

    payload = {
        "forecast_as_of": "2026-11-17T00:00:00+00:00",
        "service_level_z": {"standard": 1.28},
        "skus": [
            {
                "sku": "S",
                "name": "S",
                "pack_size": 5,
                "unit_margin": 10,
                "category": "c",
                "service_class": "standard",
            }
        ],
        "warehouses": [
            {
                "warehouse_id": "W",
                "handling_capacity_per_day": 1000,
                "region": "US",
                "country": "US",
                "last_mile_score": 1,
            }
        ],
        "lanes": [
            {
                "from": "GLOBAL-POOL",
                "to": "W",
                "lead_time_days": 10,
                "lead_time_std_days": 0,
                "unit_cost": 50,
            }
        ],
        "transport_options": [
            {
                "from": "GLOBAL-POOL",
                "to": "W",
                "mode": "AIR",
                "lead_time_days": 2,
                "unit_cost": 1,
                "max_units": 1000,
                "reliability": 1,
            },
            {
                "from": "GLOBAL-POOL",
                "to": "W",
                "mode": "SEA",
                "lead_time_days": 10,
                "unit_cost": 50,
                "max_units": 1000,
                "reliability": 1,
            },
        ],
        "inventory": [
            {
                "sku": "S",
                "warehouse_id": "W",
                "on_hand": 50,
                "in_transit": 0,
                "allocated": 0,
            }
        ],
        "global_supply_pool": [
            {"sku": "S", "available_units": 1000, "reserved_units": 0}
        ],
        "demand_history": [
            {"sku": "S", "warehouse_id": "W", "daily_units": [10] * 100}
        ],
    }
    daily = [30] * 7 + [10] * 23
    intervals = {
        str(h): {
            "expected": sum(daily[:h]),
            "p50": sum(daily[:h]),
            "p80": sum(daily[:h]),
            "p95": sum(daily[:h]),
            "calibrated": False,
            "target_start": "2026-11-17",
            "target_end": str(date(2026, 11, 17) + timedelta(days=h - 1)),
        }
        for h in (2, 7, 9, 10, 17, 30)
    }
    forecast = {
        "sku": "S",
        "warehouse_id": "W",
        "model": "rolling-champion-v3",
        "timezone": "UTC",
        "daily": daily,
        "horizons": intervals,
    }
    decision = {
        "prediction": {"rows": [forecast], "horizons": [2, 7, 9, 10, 17, 30]},
        "arrivals": {"S/W": {"arrivals": [], "unknown_inbound_units": 0}},
        "forecast_id": "fixture",
        "execution_eligible": False,
    }
    policy = PlanningPolicy(
        algorithm_version="aegis-protection-v3",
        forecast_result_id=str(uuid.uuid4()),
        forecast_model_version="rolling-champion-v3",
    )
    result = solve_protection(payload, policy, decision)
    row = result["lines"][0]
    assert row["selected_mode"] == "AIR"
    assert row["protection_days"] == 9
    assert row["target_stock"] == 230  # 7*30 +2*10; not forecast(30)/30*9.
    assert row["allocated_units"] == 180
    assert row["unavoidable_prearrival_units"] == 10
    decision["prediction"]["purchase_intents"] = [
        {
            "sku": "S",
            "warehouse_id": "W",
            "model": "rolling-champion-v3",
            "units": 100,
            "end": "2026-11-20",
        },
        {
            "sku": "S",
            "warehouse_id": "W",
            "model": "rolling-champion-v3",
            "units": 100,
            "end": "2026-11-25",
        },
    ]
    row = solve_protection(payload, policy, decision)["lines"][0]
    assert [r["units"] for r in row["purchase_requirements"]] == [100, 200]
    assert row["allocated_units"] == 200


def test_legacy_champion_keeps_asof_category_prior():
    s = DemandSeries("NEW", "W", [0, 0, 0], calendar_start="2026-01-01")
    daily, evidence = curve(
        s, "seasonal-tsb-v2", 7, [], "2026-01-04T00:00:00Z", cold_start_rate=25
    )
    assert daily == [17.5] * 7
    assert evidence["selection"]["external_prior"] == 25
    # Newer candidates deliberately do not consume peer priors.
    daily, _ = curve(
        s, "rolling-champion-v3", 7, [], "2026-01-04T00:00:00Z", cold_start_rate=25
    )
    assert daily == [0.0] * 7


def test_purchase_intent_cannot_be_bypassed_by_rejected_plan_line():
    import uuid

    from aegis.core.errors import Conflict
    from aegis.decision.protection import validate_current_capacity
    from aegis.planning.policy import PlanningPolicy

    # No approved shipments: a rejected mandatory line must still be checked.
    payload = {
        "service_level_z": {},
        "skus": [],
        "warehouses": [],
        "lanes": [],
        "inventory": [],
        "demand_history": [],
        "global_supply_pool": [],
    }
    prediction = {
        "rows": [
            {
                "sku": "S",
                "warehouse_id": "W",
                "model": "rolling-champion-v3",
                "horizons": {"7": {"target_start": "2026-11-17"}},
            }
        ],
        "purchase_intents": [
            {
                "sku": "S",
                "warehouse_id": "W",
                "model": "rolling-champion-v3",
                "units": 100,
                "end": "2026-11-23",
            }
        ],
    }
    decision = {"arrivals": {}, "prediction": prediction}
    policy = PlanningPolicy(
        algorithm_version="aegis-protection-v3",
        forecast_result_id=str(uuid.uuid4()),
        forecast_model_version="rolling-champion-v3",
    )
    with pytest.raises(Conflict, match="purchase intent"):
        validate_current_capacity(
            policy,
            decision,
            {
                "stale": False,
                "arrivals": {},
                "network": payload,
                "as_of": "2026-11-17T00:00:00Z",
            },
            [],
        )
