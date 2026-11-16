from copy import deepcopy
from dataclasses import replace
from math import ceil, sqrt
from pathlib import Path

import pytest
from overseas_inventory.models import DemandSeries, InventoryPosition, load_network
from aegis.planning.forecast import forecast, uncensor
from aegis.planning.policy import PlanningPolicy
from aegis.planning.engine import solve
from aegis.planning.backtest import rolling_backtest


@pytest.fixture
def payload():
    return {"service_level_z": {"standard": 1.28},
            "skus": [{"sku": "S", "name": "SKU", "category": "c", "pack_size": 5, "unit_margin": 10, "service_class": "standard"}],
            "warehouses": [{"warehouse_id": "W", "region": "US", "country": "US", "handling_capacity_per_day": 1000, "last_mile_score": 1}],
            "lanes": [{"from": "GLOBAL-POOL", "to": "W", "lead_time_days": 10, "lead_time_std_days": 2, "unit_cost": 1}],
            "inventory": [{"sku": "S", "warehouse_id": "W", "on_hand": 0, "in_transit": 0, "allocated": 0}],
            "demand_history": [{"sku": "S", "warehouse_id": "W", "daily_units": [10]*56}],
            "global_supply_pool": [{"sku": "S", "available_units": 500, "reserved_units": 20}],
            "transport_options": [{"from": "GLOBAL-POOL", "to": "W", "mode": "SEA", "lead_time_days": 10, "unit_cost": 1, "reliability": 1, "max_units": 1000}],
            "warehouse_constraints": [{"warehouse_id": "W", "capacity_units": 1000, "storage_cost_per_unit_day": 0.02, "max_daily_inbound_units": 500}],
            "procurement_constraints": []}


def test_negative_inventory_position_is_preserved():
    assert InventoryPosition("S", "W", 1, 2, 10, 3, 4).available_position == -8


def test_no_double_uncertainty_and_lead_variability(payload):
    row = solve(payload, PlanningPolicy())["lines"][0]
    assert row["target_stock"] == ceil(10 * 17 + 1.28 * sqrt(10**2 * 2**2))
    assert row["reorder_point"] == ceil(10 * 10 + 1.28 * 20)
    assert row["recommended_units"] % 5 == 0


def test_censoring_does_not_collapse_demand():
    s = DemandSeries("S", "W", [10]*28 + [0]*14, [False]*28 + [True]*14)
    assert uncensor(s)[0] == [10]*42
    assert forecast(s).daily_rate == 10
    assert forecast(s).confidence < forecast(DemandSeries("S", "W", [10]*42)).confidence


def test_censoring_is_causal():
    s = DemandSeries("S", "W", [10, 0, 100], [False, True, False])
    assert uncensor(s)[0][1] == 10


def test_tsb_decays_obsolete_demand():
    a = forecast(DemandSeries("S", "W", [0, 0, 20]*20))
    b = forecast(DemandSeries("S", "W", [0, 0, 20]*20 + [0]*30))
    assert a.model_name == "TSB"
    assert b.daily_rate < a.daily_rate * 0.1


def test_quantiles_and_zero_series():
    s = forecast(DemandSeries("S", "W", [0]*56))
    assert s.p50_units == s.p80_units == s.p95_units == 0


def test_frozen_input_reproduces(payload):
    assert solve(payload, PlanningPolicy(), seed=7) == solve(deepcopy(payload), PlanningPolicy(), seed=7)


@pytest.mark.parametrize("limit", [0, 1, 4, 5, 17, 100])
def test_allocation_respects_stock_route_capacity_and_packs(payload, limit):
    payload["global_supply_pool"][0]["available_units"] = limit
    payload["global_supply_pool"][0]["reserved_units"] = 0
    result = solve(payload, PlanningPolicy())
    row = result["lines"][0]
    assert row["allocated_units"] <= min(limit, row["recommended_units"])
    assert row["allocated_units"] % 5 == 0
    assert row["unmet_units"] + row["allocated_units"] == row["recommended_units"]


def test_shared_transport_quota_across_skus(payload):
    payload["skus"].append({**payload["skus"][0], "sku": "T"})
    payload["inventory"].append({**payload["inventory"][0], "sku": "T"})
    payload["demand_history"].append({**payload["demand_history"][0], "sku": "T"})
    payload["global_supply_pool"].append({**payload["global_supply_pool"][0], "sku": "T"})
    payload["transport_options"][0]["max_units"] = 55
    result = solve(payload, PlanningPolicy())
    assert sum(r["allocated_units"] for r in result["lines"]) == 55


def test_cost_objective_can_choose_no_shipment(payload):
    payload["transport_options"][0]["unit_cost"] = 10000
    result = solve(payload, PlanningPolicy())
    assert result["summary"]["allocated_units"] == 0
    assert result["optimizer"]["status"] == "optimal"


def test_moq_and_budget(payload):
    payload["procurement_constraints"] = [{"sku": "S", "supplier": "x", "available_units": 500,
                                           "min_order_qty": 100, "unit_purchase_cost": 10, "capital_budget": 900}]
    assert solve(payload, PlanningPolicy())["summary"]["allocated_units"] == 0
    payload["procurement_constraints"][0]["capital_budget"] = 1200
    assert solve(payload, PlanningPolicy())["summary"]["allocated_units"] == 120


def test_fallback_keeps_hard_constraints(payload, monkeypatch):
    import scipy.optimize
    monkeypatch.setattr(scipy.optimize, "milp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    payload["transport_options"][0]["max_units"] = 35
    result = solve(payload, PlanningPolicy())
    assert result["optimizer"]["status"] == "heuristic-fallback"
    assert result["summary"]["allocated_units"] == 35


def test_manual_pins_preserved_and_infeasible_pins_fail(payload):
    pins = [{"sku": "S", "warehouse_id": "W", "units": 15, "transport_mode": "SEA"}]
    assert solve(payload, PlanningPolicy(), pins=pins)["lines"][0]["final_units"] == 15
    pins[0]["units"] = 1000
    with pytest.raises(ValueError, match="pinned"):
        solve(payload, PlanningPolicy(), pins=pins)


def test_missing_lane_fails_instead_of_silently_skipping(payload):
    payload["lanes"] = []
    with pytest.raises(ValueError, match="missing"):
        solve(payload, PlanningPolicy())


def test_backtest_uses_disjoint_holdouts_and_zero_denominator():
    network = load_network(Path(__file__).resolve().parents[2] / "data/sample_network.json")
    result = rolling_backtest(network)
    assert result["metrics"]["overall"]["folds"] > 0
    assert {"abc:A", "abc:B", "abc:C"} <= result["metrics"].keys()
    series = [DemandSeries("S", "W", [0]*56)]
    zero = rolling_backtest(replace(network, demand_history=series, skus={"S": replace(next(iter(network.skus.values())), sku="S")}))
    assert zero["metrics"]["overall"]["wape"] is None


def test_missing_days_are_separate_from_stockout_and_held_out_of_backtests():
    s = DemandSeries("S", "W", [10]*28+[0]*7+[10]*21, missing_days=[False]*28+[True]*7+[False]*21)
    result = forecast(s)
    assert result.missing_days == 7 and result.censored_days == 0
    assert result.observed_days == 49
    assert result.daily_rate == 10
    network = load_network(Path(__file__).resolve().parents[2] / "data/sample_network.json")
    network = replace(network, demand_history=[s], skus={"S": replace(next(iter(network.skus.values())), sku="S")})
    tested = rolling_backtest(network)
    assert all(row["origin"] != 28 for row in tested["folds"])
