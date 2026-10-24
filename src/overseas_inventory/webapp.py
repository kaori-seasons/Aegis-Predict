from __future__ import annotations

import argparse
import hashlib
import hmac
import math
from datetime import UTC, datetime
from html import escape
import json
import os
from pathlib import Path
import random
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from .demo import build_plan
from .models import load_network


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = ROOT / "runs"
DEFAULT_SAMPLE_PATH = ROOT / "data" / "sample_network.json"
SESSION_COOKIE = "globalpool_session"
DEFAULT_ADMIN_PASSWORD = "globalpool"
SIMULATION_KEY_SEP = "||"
SIMULATION_HISTORY_DAYS = 180
SIMULATION_SCHEMA_VERSION = 3
SIMULATION_TICK_MINUTES = 60
SIMULATION_FORECAST_CADENCE_HOURS = 24
SIMULATION_DECISION_CADENCE_HOURS = 6
SIMULATION_MAX_RECENT_ARRIVALS = 8

SIMULATION_SCENARIOS = {
    "normal": "常规滚动",
    "black_friday": "黑五促销",
    "us_spike": "美国仓爆单",
    "eu_rebalance": "欧洲调拨",
    "au_delay": "澳洲物流延迟",
}

SIMULATION_PLATFORM_PROFILES = {
    "Amazon": {"weight": 0.52, "lift": 1.08, "cancel_rate": 0.018, "ad_cost_per_unit": 0.42},
    "TikTok Shop": {"weight": 0.18, "lift": 1.26, "cancel_rate": 0.034, "ad_cost_per_unit": 0.58},
    "eBay": {"weight": 0.16, "lift": 0.92, "cancel_rate": 0.022, "ad_cost_per_unit": 0.28},
    "Shopify": {"weight": 0.14, "lift": 1.0, "cancel_rate": 0.012, "ad_cost_per_unit": 0.22},
}

SIMULATION_WAREHOUSE_CAPACITY_PER_HOUR = {
    "US-LAX": 140,
    "DE-FRA": 96,
    "UK-BHX": 82,
    "AU-SYD": 62,
}

SIMULATION_LOCAL_HOUR_OFFSETS = {
    "US-LAX": -8,
    "DE-FRA": 1,
    "UK-BHX": 0,
    "AU-SYD": 10,
}

SIMULATION_RETURN_RATES = {
    "HUB": 0.035,
    "LAPSTAND": 0.045,
    "IPAD": 0.055,
    "APCASE": 0.05,
    "STAND": 0.038,
    "CAB": 0.018,
}

SIMULATION_PRICE_ELASTICITY = {
    "HUB": -0.8,
    "LAPSTAND": -0.95,
    "IPAD": -1.05,
    "APCASE": -1.25,
    "STAND": -1.1,
    "CAB": -1.35,
}


class SimulationDataError(ValueError):
    pass


class OperationError(ValueError):
    """Validation error for in-environment operations (approval / execution
    write-back, run lookup) — raised back to the API layer as a 400."""
    pass


def create_simulation_run(
    runs_dir: Path = DEFAULT_RUNS_DIR,
    horizon_days: int = 30,
    source_path: Path = DEFAULT_SAMPLE_PATH,
    label: str = "simulation-engine",
) -> dict:
    """Create a run directly from the simulation data engine.

    This is the sole data path: the digital-twin simulation engine
    (``data/sample_network.json`` and its random demand/inventory/in-transit
    processes) feeds :func:`build_plan`, producing the same artifacts the
    realtime sandbox consumes. There is no external upload or company-API
    ingestion any more — the system runs, demos and reproduces on its own.
    """
    if not source_path.exists():
        raise SimulationDataError(f"仿真数据源不存在：{source_path}")

    now = datetime.now(UTC)
    job_id = now.strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
    job_dir = runs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    try:
        load_network(source_path)
        plan = build_plan(source_path, job_dir, horizon_days=horizon_days)
    except Exception as exc:  # noqa: BLE001 - surface engine/validation errors to the caller.
        raise SimulationDataError(f"仿真数据校验或预测运行失败：{exc}") from exc

    metadata = {
        "job_id": job_id,
        "created_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data_source": label,
        "horizon_days": horizon_days,
        "recommendation_count": len(plan["recommendations"]),
        "allocation_count": len(plan["allocations"]),
        "dashboard_path": "dashboard.html",
        "markdown_path": "replenishment_plan.md",
        "json_path": "replenishment_plan.json",
    }
    (job_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    initialize_run_state(job_dir, plan)
    return {"metadata": metadata, "plan": plan, "job_dir": job_dir}


def initialize_run_state(job_dir: Path, plan: dict) -> dict:
    approvals = []
    for index, item in enumerate(plan.get("approval_queue", []), start=1):
        approvals.append(
            {
                "approval_id": f"A{index:03d}",
                "sku": item["sku"],
                "warehouse_id": item["warehouse_id"],
                "recommended_units": item["recommended_units"],
                "allocated_units": item["allocated_units"],
                "transport_mode": item["transport_mode"],
                "estimated_margin": item["estimated_margin"],
                "status": "待审批",
                "note": "",
                "decided_at": None,
                "decided_by": None,
            }
        )
    state = {
        "approvals": approvals,
        "execution_events": [],
        "simulation": initialize_simulation_state(plan),
    }
    save_run_state(job_dir, state)
    return state


def load_run_state(job_dir: Path) -> dict:
    state_path = job_dir / "operations_state.json"
    plan = json.loads((job_dir / "replenishment_plan.json").read_text(encoding="utf-8"))
    if not state_path.exists():
        return initialize_run_state(job_dir, plan)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if "simulation" not in state or state["simulation"].get("schema_version") != SIMULATION_SCHEMA_VERSION:
        state["simulation"] = initialize_simulation_state(plan)
        save_run_state(job_dir, state)
    return state


def save_run_state(job_dir: Path, state: dict) -> None:
    (job_dir / "operations_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_approval_state(job_dir: Path, approval_id: str, decision: str, actor: str, note: str = "") -> dict:
    state = load_run_state(job_dir)
    if decision not in {"批准", "驳回"}:
        raise OperationError("审批动作必须是批准或驳回。")
    for approval in state["approvals"]:
        if approval["approval_id"] == approval_id:
            approval["status"] = decision
            approval["note"] = note
            approval["decided_by"] = actor
            approval["decided_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            save_run_state(job_dir, state)
            return approval
    raise OperationError("找不到该审批单。")


def append_execution_event(
    job_dir: Path,
    actor: str,
    sku: str,
    warehouse_id: str,
    executed_units: int,
    execution_status: str,
    note: str,
) -> dict:
    if not sku or not warehouse_id:
        raise OperationError("执行回写必须填写 SKU 和仓库。")
    if executed_units <= 0:
        raise OperationError("执行数量必须大于 0。")
    state = load_run_state(job_dir)
    event = {
        "event_id": "E" + uuid4().hex[:8],
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "created_by": actor,
        "sku": sku,
        "warehouse_id": warehouse_id,
        "executed_units": executed_units,
        "execution_status": execution_status,
        "note": note,
    }
    state["execution_events"].append(event)
    save_run_state(job_dir, state)
    return event


def initialize_simulation_state(plan: dict) -> dict:
    inventory = {
        simulation_key(row["sku"], row["warehouse_id"]): int(row.get("inventory_position", 0))
        for row in plan.get("recommendations", [])
    }
    state = {
        "running": False,
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "scenario": "normal",
        "scenario_label": SIMULATION_SCENARIOS["normal"],
        "history_days": SIMULATION_HISTORY_DAYS,
        "forecast_cadence_hours": SIMULATION_FORECAST_CADENCE_HOURS,
        "decision_cadence_hours": SIMULATION_DECISION_CADENCE_HOURS,
        "tick": 0,
        "simulated_minutes": 0,
        "current_sim_day": SIMULATION_HISTORY_DAYS + 1,
        "simulation_clock": simulation_time_label(0),
        "started_at": None,
        "updated_at": None,
        "inventory": inventory,
        "in_transit_shipments": [],
        "arrivals": [],
        "orders_total": 0,
        "gross_order_units": 0,
        "units_requested": 0,
        "units_fulfilled": 0,
        "units_stockout": 0,
        "units_cancelled": 0,
        "units_delayed": 0,
        "units_returned": 0,
        "restocked_return_units": 0,
        "revenue": 0.0,
        "gross_margin": 0.0,
        "ad_spend": 0.0,
        "lost_revenue": 0.0,
        "forecast_version": 1,
        "decision_version": 1,
        "last_forecast_tick": 0,
        "last_decision_tick": 0,
        "next_forecast_in_hours": SIMULATION_FORECAST_CADENCE_HOURS,
        "next_decision_in_hours": SIMULATION_DECISION_CADENCE_HOURS,
        "latest_forecast_summary": [],
        "decision_status": "初始策略来自 180 天历史模拟数据",
        "recent_orders": [],
        "fulfillment_backlog": [],
        "pending_returns": [],
        "time_series": [],
        "events": [],
        "warehouse_metrics": [],
        "dynamic_recommendations": [],
        "active_incidents": [],
        "market_pulse": initial_market_pulse(),
    }
    append_simulation_event(state, "历史基线", "已生成 180 天历史订单、库存、补货和物流数据，作为机器学习预测训练集")
    append_simulation_event(state, "仿真待启动", "实时沙盘将从第 181 天开始：订单逐小时进入，预测每日刷新，补货决策每 6 小时滚动")
    update_rolling_forecast_summary(plan, state, announce=False)
    refresh_simulation_views(plan, state)
    return state


def start_simulation(job_dir: Path, scenario: str = "normal") -> dict:
    plan = json.loads((job_dir / "replenishment_plan.json").read_text(encoding="utf-8"))
    state = load_run_state(job_dir)
    simulation = state.setdefault("simulation", initialize_simulation_state(plan))
    scenario = normalize_scenario(scenario)
    now = utc_now()
    simulation["running"] = True
    simulation["scenario"] = scenario
    simulation["scenario_label"] = SIMULATION_SCENARIOS[scenario]
    simulation["started_at"] = simulation.get("started_at") or now
    simulation["updated_at"] = now
    append_simulation_event(
        simulation,
        "系统",
        f"实时经营沙盘已启动：{SIMULATION_SCENARIOS[scenario]}；现实 1.8 秒 = 模拟 1 小时",
    )
    refresh_simulation_views(plan, simulation)
    save_run_state(job_dir, state)
    return simulation


def stop_simulation(job_dir: Path) -> dict:
    plan = json.loads((job_dir / "replenishment_plan.json").read_text(encoding="utf-8"))
    state = load_run_state(job_dir)
    simulation = state.setdefault("simulation", initialize_simulation_state(plan))
    simulation["running"] = False
    simulation["updated_at"] = utc_now()
    append_simulation_event(simulation, "系统", "实时经营沙盘已暂停")
    refresh_simulation_views(plan, simulation)
    save_run_state(job_dir, state)
    return simulation


def reset_simulation(job_dir: Path) -> dict:
    plan = json.loads((job_dir / "replenishment_plan.json").read_text(encoding="utf-8"))
    state = load_run_state(job_dir)
    state["simulation"] = initialize_simulation_state(plan)
    append_simulation_event(state["simulation"], "系统", "实时经营沙盘已重置到 180 天历史数据训练后的第 181 天基线")
    save_run_state(job_dir, state)
    return state["simulation"]


def tick_simulation(job_dir: Path, steps: int = 1, scenario: str | None = None) -> dict:
    plan = json.loads((job_dir / "replenishment_plan.json").read_text(encoding="utf-8"))
    state = load_run_state(job_dir)
    simulation = state.setdefault("simulation", initialize_simulation_state(plan))
    if scenario:
        simulation["scenario"] = normalize_scenario(scenario)
        simulation["scenario_label"] = SIMULATION_SCENARIOS[simulation["scenario"]]
    steps = max(1, min(int(steps), 12))
    for _ in range(steps):
        run_simulation_step(plan, simulation)
    simulation["updated_at"] = utc_now()
    refresh_simulation_views(plan, simulation, update_recommendations=False)
    save_run_state(job_dir, state)
    return simulation


def run_simulation_step(plan: dict, simulation: dict) -> None:
    scenario = normalize_scenario(simulation.get("scenario", "normal"))
    simulation["scenario"] = scenario
    simulation["scenario_label"] = SIMULATION_SCENARIOS[scenario]
    simulation["tick"] = int(simulation.get("tick", 0)) + 1
    simulation["simulated_minutes"] = int(simulation.get("simulated_minutes", 0)) + SIMULATION_TICK_MINUTES
    refresh_simulation_clock(simulation)
    capacity_start = warehouse_capacity_snapshot(plan)
    capacity_remaining = dict(capacity_start)
    capacity_used = process_fulfillment_backlog(plan, simulation, capacity_remaining)
    process_pending_returns(simulation)
    process_in_transit_shipments(simulation)
    rng = random.Random(f"{simulation['tick']}:{scenario}:{simulation.get('started_at') or 'seed'}")
    orders = []
    contexts = []
    incident = scenario_incident(scenario, simulation["tick"])
    if incident:
        append_simulation_event(simulation, incident["type"], incident["message"])

    for row in plan.get("recommendations", []):
        sku = row["sku"]
        warehouse_id = row["warehouse_id"]
        context = demand_context(plan, simulation, sku, warehouse_id, rng)
        contexts.append(context)
        multiplier = context["demand_lift"]
        expected_units = max(0.0, float(row.get("daily_rate", 0)) / 24 * multiplier)
        jitter = rng.uniform(0.55, 1.65)
        units = int(round(expected_units * jitter))
        if units <= 0 and rng.random() < min(0.45, expected_units):
            units = 1
        if units <= 0:
            continue

        key = simulation_key(sku, warehouse_id)
        before = int(simulation["inventory"].get(key, row.get("inventory_position", 0)))
        cancelled = min(units, int(round(units * context["cancellation_rate"] * rng.uniform(0.35, 1.65))))
        requested_units = max(0, units - cancelled)
        fulfillable = min(before, requested_units)
        available_capacity = max(0, int(capacity_remaining.get(warehouse_id, 10**9)))
        fulfilled = min(fulfillable, available_capacity)
        delayed = max(0, fulfillable - fulfilled)
        stockout = max(0, requested_units - fulfillable)
        after = before - fulfilled
        simulation["inventory"][key] = after
        capacity_remaining[warehouse_id] = max(0, available_capacity - fulfilled)
        capacity_used[warehouse_id] = capacity_used.get(warehouse_id, 0) + fulfilled
        if delayed:
            queue_fulfillment_backlog(simulation, sku, warehouse_id, delayed, context, row)
        expected_return_units = 0
        if fulfilled:
            expected_return_units = min(fulfilled, int(round(fulfilled * context["return_rate"] * rng.uniform(0.45, 1.55))))
            if expected_return_units:
                queue_pending_return(simulation, sku, warehouse_id, expected_return_units, context, rng)
        unit_price = estimated_unit_price(sku) * context["price_index"]
        revenue = round(fulfilled * unit_price, 2)
        ad_spend = round(units * context["ad_cost_per_unit"] * context["ad_spend_index"], 2)
        margin = round(fulfilled * estimated_unit_margin(plan, sku) - ad_spend, 2)
        order = {
            "event_id": "O" + uuid4().hex[:8],
            "tick": simulation["tick"],
            "simulated_time": simulation_time_label(simulation["simulated_minutes"]),
            "sku": sku,
            "warehouse_id": warehouse_id,
            "platform": context["platform"],
            "promotion": context["promotion_label"],
            "traffic_index": context["traffic_index"],
            "ad_spend_index": context["ad_spend_index"],
            "price_index": context["price_index"],
            "local_hour": context["local_hour"],
            "demand_lift": round(multiplier, 2),
            "requested_units": requested_units,
            "gross_units": units,
            "cancelled_units": cancelled,
            "fulfilled_units": fulfilled,
            "delayed_units": delayed,
            "stockout_units": stockout,
            "expected_return_units": expected_return_units,
            "inventory_after": after,
            "revenue": revenue,
            "gross_margin": margin,
            "scenario": SIMULATION_SCENARIOS[scenario],
        }
        orders.append(order)
        simulation["orders_total"] = int(simulation.get("orders_total", 0)) + 1
        simulation["gross_order_units"] = int(simulation.get("gross_order_units", 0)) + units
        simulation["units_requested"] = int(simulation.get("units_requested", 0)) + requested_units
        simulation["units_fulfilled"] = int(simulation.get("units_fulfilled", 0)) + fulfilled
        simulation["units_stockout"] = int(simulation.get("units_stockout", 0)) + stockout
        simulation["units_cancelled"] = int(simulation.get("units_cancelled", 0)) + cancelled
        simulation["units_delayed"] = int(simulation.get("units_delayed", 0)) + delayed
        simulation["revenue"] = round(float(simulation.get("revenue", 0)) + revenue, 2)
        simulation["gross_margin"] = round(float(simulation.get("gross_margin", 0)) + margin, 2)
        simulation["ad_spend"] = round(float(simulation.get("ad_spend", 0)) + ad_spend, 2)
        simulation["lost_revenue"] = round(float(simulation.get("lost_revenue", 0)) + stockout * unit_price, 2)

    update_market_pulse(simulation, contexts, orders, capacity_start, capacity_used)
    append_simulation_time_series(simulation)
    recent = sorted(orders, key=lambda item: (item["stockout_units"], item["delayed_units"], item["requested_units"]), reverse=True)[:18]
    simulation["recent_orders"] = (recent + simulation.get("recent_orders", []))[:36]
    decision_due = simulation["tick"] % SIMULATION_DECISION_CADENCE_HOURS == 0
    forecast_due = simulation["tick"] % SIMULATION_FORECAST_CADENCE_HOURS == 0
    refresh_simulation_views(plan, simulation, update_recommendations=decision_due)
    if forecast_due:
        update_rolling_forecast_summary(plan, simulation, announce=True)
    if decision_due:
        run_replenishment_decision(plan, simulation)
    refresh_cadence_fields(simulation)


def refresh_simulation_views(plan: dict, simulation: dict, update_recommendations: bool = True) -> None:
    inventory = simulation.setdefault("inventory", {})
    demand_by_warehouse: dict[str, int] = {}
    stockout_by_warehouse: dict[str, int] = {}
    requested_by_warehouse: dict[str, int] = {}
    inbound_by_key = active_inbound_by_key(simulation)
    inbound_by_warehouse: dict[str, int] = {}
    for key, units in inbound_by_key.items():
        _, warehouse_id = key.split(SIMULATION_KEY_SEP, 1)
        inbound_by_warehouse[warehouse_id] = inbound_by_warehouse.get(warehouse_id, 0) + units
    for order in simulation.get("recent_orders", [])[:24]:
        warehouse_id = order["warehouse_id"]
        demand_by_warehouse[warehouse_id] = demand_by_warehouse.get(warehouse_id, 0) + int(order["fulfilled_units"])
        stockout_by_warehouse[warehouse_id] = stockout_by_warehouse.get(warehouse_id, 0) + int(order["stockout_units"])
        requested_by_warehouse[warehouse_id] = requested_by_warehouse.get(warehouse_id, 0) + int(order["requested_units"])

    metrics = []
    recommendations = []
    for warehouse in plan.get("warehouse_map", []):
        warehouse_id = warehouse["warehouse_id"]
        related = [row for row in plan.get("recommendations", []) if row["warehouse_id"] == warehouse_id]
        current_inventory = sum(
            int(inventory.get(simulation_key(row["sku"], warehouse_id), row.get("inventory_position", 0)))
            for row in related
        )
        daily_rate = sum(float(row.get("daily_rate", 0)) * scenario_multiplier(simulation.get("scenario", "normal"), row["sku"], warehouse_id) for row in related)
        days_of_supply = round(current_inventory / max(1.0, daily_rate), 1)
        stockout_units = stockout_by_warehouse.get(warehouse_id, 0)
        requested_units = requested_by_warehouse.get(warehouse_id, 0)
        risk = simulation_risk(days_of_supply, stockout_units, requested_units)
        metrics.append(
            {
                "warehouse_id": warehouse_id,
                "current_inventory": current_inventory,
                "inbound_units": inbound_by_warehouse.get(warehouse_id, 0),
                "hourly_demand": round(daily_rate / 24, 1),
                "days_of_supply": days_of_supply,
                "risk": risk,
                "stockout_units": stockout_units,
                "fill_rate": round(1 - stockout_units / max(1, requested_units), 3),
                "capacity_load": simulation.get("market_pulse", {}).get("capacity_load", {}).get(warehouse_id, 0),
                "backlog_units": sum(
                    int(item.get("quantity", 0))
                    for item in simulation.get("fulfillment_backlog", [])
                    if item.get("warehouse_id") == warehouse_id
                ),
            }
        )

    if update_recommendations or not simulation.get("dynamic_recommendations"):
        for row in plan.get("recommendations", []):
            key = simulation_key(row["sku"], row["warehouse_id"])
            current_inventory = int(inventory.get(key, row.get("inventory_position", 0)))
            inbound_units = inbound_by_key.get(key, 0)
            multiplier = scenario_multiplier(simulation.get("scenario", "normal"), row["sku"], row["warehouse_id"])
            target = int(round(row.get("target_stock", 0) * scenario_target_multiplier(simulation.get("scenario", "normal"), row["warehouse_id"])))
            gap = max(0, target - current_inventory - inbound_units)
            projected_daily = round(float(row.get("daily_rate", 0)) * multiplier, 1)
            if gap <= 0 and (current_inventory + inbound_units) / max(1.0, projected_daily) >= 7:
                continue
            recommendations.append(
                {
                    "sku": row["sku"],
                    "warehouse_id": row["warehouse_id"],
                    "current_inventory": current_inventory,
                    "inbound_units": inbound_units,
                    "projected_daily_rate": projected_daily,
                    "suggested_units": gap,
                    "urgency": simulation_risk((current_inventory + inbound_units) / max(1.0, projected_daily), 0, 1),
                    "reason": dynamic_recommendation_reason(simulation.get("scenario", "normal"), row["warehouse_id"], gap),
                }
            )

    simulation["warehouse_metrics"] = sorted(metrics, key=lambda item: item["risk"], reverse=True)
    if update_recommendations or not simulation.get("dynamic_recommendations"):
        simulation["dynamic_recommendations"] = sorted(
            recommendations,
            key=lambda item: (item["urgency"], item["suggested_units"]),
            reverse=True,
        )[:12]
    total_requested = int(simulation.get("units_requested", 0))
    total_stockout = int(simulation.get("units_stockout", 0))
    simulation["fill_rate"] = round(1 - total_stockout / max(1, total_requested), 3)
    simulation["service_rate"] = round(int(simulation.get("units_fulfilled", 0)) / max(1, total_requested), 3)
    simulation["active_incidents"] = active_incidents(simulation.get("scenario", "normal"))
    refresh_simulation_clock(simulation)
    refresh_cadence_fields(simulation)


def simulation_key(sku: str, warehouse_id: str) -> str:
    return f"{sku}{SIMULATION_KEY_SEP}{warehouse_id}"


def normalize_scenario(scenario: str) -> str:
    return scenario if scenario in SIMULATION_SCENARIOS else "normal"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def simulation_time_label(minutes: int, history_days: int = SIMULATION_HISTORY_DAYS) -> str:
    day = history_days + minutes // 1440 + 1
    hour = (minutes % 1440) // 60
    return f"D{day} {hour:02d}:00"


def refresh_simulation_clock(simulation: dict) -> None:
    history_days = int(simulation.get("history_days", SIMULATION_HISTORY_DAYS))
    minutes = int(simulation.get("simulated_minutes", 0))
    simulation["current_sim_day"] = history_days + minutes // 1440 + 1
    simulation["simulation_clock"] = simulation_time_label(minutes, history_days=history_days)


def refresh_cadence_fields(simulation: dict) -> None:
    tick = int(simulation.get("tick", 0))
    forecast_last = int(simulation.get("last_forecast_tick", 0))
    decision_last = int(simulation.get("last_decision_tick", 0))
    forecast_remaining = SIMULATION_FORECAST_CADENCE_HOURS - max(0, tick - forecast_last)
    decision_remaining = SIMULATION_DECISION_CADENCE_HOURS - max(0, tick - decision_last)
    simulation["next_forecast_in_hours"] = max(1, forecast_remaining)
    simulation["next_decision_in_hours"] = max(1, decision_remaining)


def initial_market_pulse() -> dict:
    return {
        "traffic_index": 100,
        "promotion_intensity": 1.0,
        "ad_spend_index": 1.0,
        "price_index": 1.0,
        "platform_mix": {},
        "capacity_load": {},
        "backlog_units": 0,
        "pending_return_units": 0,
        "cancelled_units": 0,
        "returned_units": 0,
    }


def demand_context(plan: dict, simulation: dict, sku: str, warehouse_id: str, rng: random.Random) -> dict:
    scenario = normalize_scenario(simulation.get("scenario", "normal"))
    signal = market_signal_for(plan, sku, warehouse_id)
    platform = choose_platform(rng, scenario)
    platform_profile = SIMULATION_PLATFORM_PROFILES[platform]
    local_hour = local_hour_for(simulation, warehouse_id)
    hour_lift = intraday_lift(local_hour)
    weekday_lift = weekday_lift_for(simulation)
    scenario_lift = scenario_multiplier(scenario, sku, warehouse_id)
    campaign_lift = scenario_campaign_lift(scenario, sku, warehouse_id, int(simulation.get("tick", 0)))
    ad_spend_index = float(signal.get("ad_spend_index", 1.0)) * campaign_lift["ad"]
    promotion_lift = float(signal.get("promotion_lift", 1.0)) * campaign_lift["promotion"]
    seasonal_index = float(signal.get("seasonal_index", 1.0))
    platform_event_lift = float(signal.get("platform_event_lift", 1.0)) * float(platform_profile["lift"])
    price_index = max(0.72, min(1.35, float(signal.get("price_index", 1.0)) * campaign_lift["price"]))
    price_lift = price_index ** price_elasticity_for(sku)
    traffic_index = int(round(100 * hour_lift * weekday_lift * ad_spend_index * platform_event_lift))
    demand_lift = (
        scenario_lift
        * hour_lift
        * weekday_lift
        * promotion_lift
        * seasonal_index
        * platform_event_lift
        * price_lift
    )
    cancellation_rate = min(0.12, float(platform_profile["cancel_rate"]) + campaign_lift["cancel_add"])
    return {
        "sku": sku,
        "warehouse_id": warehouse_id,
        "platform": platform,
        "promotion_label": campaign_lift["label"],
        "traffic_index": traffic_index,
        "ad_spend_index": round(ad_spend_index, 2),
        "promotion_lift": round(promotion_lift, 2),
        "seasonal_index": round(seasonal_index, 2),
        "platform_event_lift": round(platform_event_lift, 2),
        "price_index": round(price_index, 2),
        "demand_lift": round(max(0.15, demand_lift), 3),
        "cancellation_rate": cancellation_rate,
        "return_rate": return_rate_for(sku, scenario),
        "ad_cost_per_unit": float(platform_profile["ad_cost_per_unit"]),
        "local_hour": local_hour,
    }


def market_signal_for(plan: dict, sku: str, warehouse_id: str) -> dict:
    signals = plan.get("simulation_inputs", {}).get("market_signals", {})
    signal = signals.get(simulation_key(sku, warehouse_id), {})
    if signal:
        return signal
    return {
        "ad_spend_index": 1.08 if sku.startswith(("HUB", "STAND")) else 1.0,
        "promotion_lift": 1.12 if sku.startswith(("CAB", "APCASE")) else 1.04,
        "seasonal_index": 1.02,
        "platform_event_lift": 1.03,
        "price_index": 1.0,
    }


def choose_platform(rng: random.Random, scenario: str) -> str:
    weights = []
    for platform, profile in SIMULATION_PLATFORM_PROFILES.items():
        weight = float(profile["weight"])
        if scenario == "black_friday" and platform in {"Amazon", "TikTok Shop"}:
            weight *= 1.25
        if scenario == "us_spike" and platform == "Amazon":
            weight *= 1.18
        weights.append((platform, weight))
    total = sum(weight for _, weight in weights)
    point = rng.random() * total
    running = 0.0
    for platform, weight in weights:
        running += weight
        if point <= running:
            return platform
    return weights[-1][0]


def local_hour_for(simulation: dict, warehouse_id: str) -> int:
    minutes = int(simulation.get("simulated_minutes", 0))
    utc_hour = (minutes % 1440) // 60
    return int((utc_hour + SIMULATION_LOCAL_HOUR_OFFSETS.get(warehouse_id, 0)) % 24)


def intraday_lift(local_hour: int) -> float:
    if 0 <= local_hour <= 5:
        return 0.42
    if 6 <= local_hour <= 8:
        return 0.72
    if 9 <= local_hour <= 11:
        return 1.02
    if 12 <= local_hour <= 16:
        return 1.18
    if 17 <= local_hour <= 22:
        return 1.42
    return 0.86


def weekday_lift_for(simulation: dict) -> float:
    day_index = int(simulation.get("current_sim_day", SIMULATION_HISTORY_DAYS + 1))
    weekday = day_index % 7
    if weekday in {5, 6}:
        return 1.16
    if weekday == 1:
        return 0.93
    return 1.0


def scenario_campaign_lift(scenario: str, sku: str, warehouse_id: str, tick: int) -> dict:
    hour_in_cycle = tick % 24
    pulse = 1.0 + 0.08 * math.sin((hour_in_cycle / 24) * math.tau)
    result = {"label": "常规自然流量", "promotion": 1.0, "ad": pulse, "price": 1.0, "cancel_add": 0.0}
    if scenario == "black_friday":
        result.update({"label": "黑五平台活动", "promotion": 1.55, "ad": 1.28 * pulse, "price": 0.94, "cancel_add": 0.012})
        if sku.startswith("HUB"):
            result["promotion"] *= 1.12
    elif scenario == "us_spike" and warehouse_id == "US-LAX":
        result.update({"label": "美国爆单流量", "promotion": 1.18, "ad": 1.18 * pulse, "price": 0.98, "cancel_add": 0.006})
    elif scenario == "eu_rebalance":
        if warehouse_id == "UK-BHX":
            result.update({"label": "英国站内转化上升", "promotion": 1.2, "ad": 1.12 * pulse, "price": 0.99, "cancel_add": 0.004})
        elif warehouse_id == "DE-FRA":
            result.update({"label": "德国需求放缓", "promotion": 0.92, "ad": 0.86 * pulse, "price": 1.03, "cancel_add": 0.0})
    elif scenario == "au_delay" and warehouse_id == "AU-SYD":
        result.update({"label": "澳洲物流风险备货", "promotion": 1.08, "ad": 1.04 * pulse, "price": 1.02, "cancel_add": 0.008})
    return result


def price_elasticity_for(sku: str) -> float:
    for prefix, elasticity in SIMULATION_PRICE_ELASTICITY.items():
        if sku.startswith(prefix):
            return elasticity
    return -1.0


def return_rate_for(sku: str, scenario: str) -> float:
    rate = 0.03
    for prefix, value in SIMULATION_RETURN_RATES.items():
        if sku.startswith(prefix):
            rate = value
            break
    if scenario == "black_friday":
        rate *= 1.18
    return min(0.12, rate)


def warehouse_capacity_snapshot(plan: dict) -> dict[str, int]:
    capacities = dict(SIMULATION_WAREHOUSE_CAPACITY_PER_HOUR)
    warehouses = plan.get("simulation_inputs", {}).get("warehouses", {})
    for warehouse_id, warehouse in warehouses.items():
        daily = int(warehouse.get("handling_capacity_per_day", 0) or 0)
        if daily > 0:
            capacities[warehouse_id] = max(35, min(260, round(daily / 24 * 0.42)))
    return capacities


def process_fulfillment_backlog(plan: dict, simulation: dict, capacity_remaining: dict[str, int]) -> dict[str, int]:
    inventory = simulation.setdefault("inventory", {})
    active = []
    used: dict[str, int] = {}
    tick = int(simulation.get("tick", 0))
    for item in simulation.get("fulfillment_backlog", []):
        if int(item.get("due_tick", 0)) > tick:
            active.append(item)
            continue
        sku = item["sku"]
        warehouse_id = item["warehouse_id"]
        quantity = int(item.get("quantity", 0))
        if quantity <= 0:
            continue
        key = simulation_key(sku, warehouse_id)
        before = int(inventory.get(key, 0))
        available_capacity = max(0, int(capacity_remaining.get(warehouse_id, 0)))
        fulfilled = min(before, quantity, available_capacity)
        if fulfilled:
            inventory[key] = before - fulfilled
            capacity_remaining[warehouse_id] = available_capacity - fulfilled
            used[warehouse_id] = used.get(warehouse_id, 0) + fulfilled
            price = estimated_unit_price(sku)
            simulation["units_fulfilled"] = int(simulation.get("units_fulfilled", 0)) + fulfilled
            simulation["revenue"] = round(float(simulation.get("revenue", 0)) + fulfilled * price, 2)
            simulation["gross_margin"] = round(float(simulation.get("gross_margin", 0)) + fulfilled * estimated_unit_margin(plan, sku), 2)
        remaining = quantity - fulfilled
        if remaining > 0 and int(item.get("attempts", 0)) < 6:
            item["quantity"] = remaining
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["due_tick"] = tick + 1
            active.append(item)
        elif remaining > 0:
            simulation["units_stockout"] = int(simulation.get("units_stockout", 0)) + remaining
            simulation["lost_revenue"] = round(float(simulation.get("lost_revenue", 0)) + remaining * estimated_unit_price(sku), 2)
        if fulfilled:
            append_simulation_event(
                simulation,
                "延迟履约",
                f"{simulation.get('simulation_clock')} {warehouse_or_pool_label(plan, warehouse_id)} 消化前序待发 {fulfilled} 件",
            )
    simulation["fulfillment_backlog"] = active[:80]
    return used


def queue_fulfillment_backlog(simulation: dict, sku: str, warehouse_id: str, quantity: int, context: dict, row: dict) -> None:
    simulation.setdefault("fulfillment_backlog", []).append(
        {
            "backlog_id": "B" + uuid4().hex[:8],
            "created_tick": simulation.get("tick", 0),
            "due_tick": int(simulation.get("tick", 0)) + (2 if context["traffic_index"] >= 180 else 1),
            "sku": sku,
            "warehouse_id": warehouse_id,
            "quantity": int(quantity),
            "attempts": 0,
            "reason": "仓库小时处理能力不足，进入待发队列",
            "daily_rate": row.get("daily_rate", 0),
        }
    )


def process_pending_returns(simulation: dict) -> None:
    inventory = simulation.setdefault("inventory", {})
    active = []
    tick = int(simulation.get("tick", 0))
    for item in simulation.get("pending_returns", []):
        if int(item.get("due_tick", 0)) > tick:
            item["remaining_hours"] = int(item.get("due_tick", tick)) - tick
            active.append(item)
            continue
        quantity = int(item.get("quantity", 0))
        restockable = int(round(quantity * float(item.get("restockable_rate", 0.82))))
        key = simulation_key(item["sku"], item["warehouse_id"])
        inventory[key] = int(inventory.get(key, 0)) + restockable
        simulation["units_returned"] = int(simulation.get("units_returned", 0)) + quantity
        simulation["restocked_return_units"] = int(simulation.get("restocked_return_units", 0)) + restockable
        append_simulation_event(
            simulation,
            "退货入库",
            f"{simulation.get('simulation_clock')} 收到 {quantity} 件退货，其中 {restockable} 件可重新入库",
        )
    simulation["pending_returns"] = active[:120]


def queue_pending_return(
    simulation: dict,
    sku: str,
    warehouse_id: str,
    quantity: int,
    context: dict,
    rng: random.Random,
) -> None:
    due_hours = rng.randint(36, 120)
    simulation.setdefault("pending_returns", []).append(
        {
            "return_id": "R" + uuid4().hex[:8],
            "created_tick": simulation.get("tick", 0),
            "due_tick": int(simulation.get("tick", 0)) + due_hours,
            "remaining_hours": due_hours,
            "sku": sku,
            "warehouse_id": warehouse_id,
            "quantity": int(quantity),
            "platform": context["platform"],
            "restockable_rate": 0.86 if sku.startswith(("CAB", "APCASE")) else 0.78,
        }
    )


def update_market_pulse(
    simulation: dict,
    contexts: list[dict],
    orders: list[dict],
    capacity_start: dict[str, int],
    capacity_used: dict[str, int],
) -> None:
    if not contexts:
        simulation["market_pulse"] = initial_market_pulse()
        return
    platform_units: dict[str, int] = {}
    for order in orders:
        platform = order.get("platform", "Amazon")
        platform_units[platform] = platform_units.get(platform, 0) + int(order.get("gross_units", 0))
    total_platform_units = max(1, sum(platform_units.values()))
    capacity_load = {
        warehouse_id: round(capacity_used.get(warehouse_id, 0) / max(1, capacity), 2)
        for warehouse_id, capacity in capacity_start.items()
    }
    simulation["market_pulse"] = {
        "traffic_index": round(sum(item["traffic_index"] for item in contexts) / len(contexts)),
        "promotion_intensity": round(sum(item["promotion_lift"] for item in contexts) / len(contexts), 2),
        "ad_spend_index": round(sum(item["ad_spend_index"] for item in contexts) / len(contexts), 2),
        "price_index": round(sum(item["price_index"] for item in contexts) / len(contexts), 2),
        "platform_mix": {
            platform: round(units / total_platform_units * 100, 1)
            for platform, units in sorted(platform_units.items(), key=lambda item: item[1], reverse=True)
        },
        "capacity_load": capacity_load,
        "backlog_units": sum(int(item.get("quantity", 0)) for item in simulation.get("fulfillment_backlog", [])),
        "pending_return_units": sum(int(item.get("quantity", 0)) for item in simulation.get("pending_returns", [])),
        "cancelled_units": int(simulation.get("units_cancelled", 0)),
        "returned_units": int(simulation.get("units_returned", 0)),
    }


def append_simulation_time_series(simulation: dict) -> None:
    requested = int(simulation.get("units_requested", 0))
    fulfilled = int(simulation.get("units_fulfilled", 0))
    stockout = int(simulation.get("units_stockout", 0))
    inventory_total = sum(int(value) for value in simulation.get("inventory", {}).values())
    pulse = simulation.get("market_pulse", {})
    point = {
        "tick": int(simulation.get("tick", 0)),
        "clock": simulation.get("simulation_clock", simulation_time_label(0)),
        "traffic_index": int(pulse.get("traffic_index", 100)),
        "fill_rate": round(fulfilled / max(1, requested), 3),
        "stockout_units": stockout,
        "backlog_units": int(pulse.get("backlog_units", 0)),
        "pending_return_units": int(pulse.get("pending_return_units", 0)),
        "gross_margin": round(float(simulation.get("gross_margin", 0)), 2),
        "revenue": round(float(simulation.get("revenue", 0)), 2),
        "inventory_total": inventory_total,
    }
    series = simulation.setdefault("time_series", [])
    if series and series[-1].get("tick") == point["tick"]:
        series[-1] = point
    else:
        series.append(point)
    del series[:-48]


def update_rolling_forecast_summary(plan: dict, simulation: dict, announce: bool) -> None:
    if announce:
        simulation["forecast_version"] = int(simulation.get("forecast_version", 1)) + 1
        simulation["last_forecast_tick"] = int(simulation.get("tick", 0))
        append_simulation_event(
            simulation,
            "滚动预测",
            f"{simulation.get('simulation_clock', simulation_time_label(0))} 基于最新实时订单刷新未来 7/14/30 天销量预测",
        )

    inventory = simulation.setdefault("inventory", {})
    rows = []
    for row in plan.get("recommendations", []):
        warehouse_id = row["warehouse_id"]
        sku = row["sku"]
        multiplier = scenario_multiplier(simulation.get("scenario", "normal"), sku, warehouse_id)
        daily_rate = float(row.get("daily_rate", 0)) * multiplier
        current_inventory = int(inventory.get(simulation_key(sku, warehouse_id), row.get("inventory_position", 0)))
        rows.append(
            {
                "sku": sku,
                "warehouse_id": warehouse_id,
                "forecast_7d": int(round(daily_rate * 7)),
                "forecast_14d": int(round(daily_rate * 14)),
                "forecast_30d": int(round(daily_rate * 30)),
                "projected_daily_rate": round(daily_rate, 1),
                "current_inventory": current_inventory,
                "risk": simulation_risk(current_inventory / max(1.0, daily_rate), 0, 1),
            }
        )
    simulation["latest_forecast_summary"] = sorted(rows, key=lambda item: (item["risk"], item["forecast_30d"]), reverse=True)[:8]
    refresh_cadence_fields(simulation)


def run_replenishment_decision(plan: dict, simulation: dict) -> None:
    simulation["decision_version"] = int(simulation.get("decision_version", 1)) + 1
    simulation["last_decision_tick"] = int(simulation.get("tick", 0))
    shipments = create_replenishment_shipments(plan, simulation)
    if shipments:
        units = sum(int(item["quantity"]) for item in shipments)
        simulation["decision_status"] = f"已生成 {len(shipments)} 条在途补货，共 {units} 件"
        append_simulation_event(
            simulation,
            "补货决策",
            f"{simulation.get('simulation_clock')} 第 {simulation['decision_version']} 版补货分仓决策生成 {len(shipments)} 条在途任务",
        )
    else:
        simulation["decision_status"] = "当前在途和库存可覆盖目标水位"
        append_simulation_event(
            simulation,
            "补货决策",
            f"{simulation.get('simulation_clock')} 滚动复核完成，当前暂无新增补货任务",
        )
    refresh_cadence_fields(simulation)


def create_replenishment_shipments(plan: dict, simulation: dict) -> list[dict]:
    active_keys = {
        (item.get("sku"), item.get("destination_warehouse_id"))
        for item in simulation.get("in_transit_shipments", [])
        if item.get("status") == "在途"
    }
    inventory = simulation.setdefault("inventory", {})
    created: list[dict] = []
    for recommendation in simulation.get("dynamic_recommendations", [])[:8]:
        sku = recommendation["sku"]
        warehouse_id = recommendation["warehouse_id"]
        suggested_units = int(recommendation.get("suggested_units", 0))
        if suggested_units <= 0 or (sku, warehouse_id) in active_keys:
            continue

        source_id = shipment_source_for(simulation.get("scenario", "normal"), sku, warehouse_id)
        source_label = warehouse_or_pool_label(plan, source_id)
        mode = transport_mode_for(simulation.get("scenario", "normal"), sku, warehouse_id, float(recommendation.get("urgency", 0)), source_id)
        quantity = min(max(20, suggested_units), 420)
        if source_id not in {"GLOBAL-POOL", "SZ-CENTER"}:
            source_key = simulation_key(sku, source_id)
            source_inventory = int(inventory.get(source_key, 0))
            transferable = max(0, source_inventory // 4)
            quantity = min(quantity, transferable)
            if quantity <= 0:
                continue
            inventory[source_key] = source_inventory - quantity

        lead_hours = simulated_lead_time_hours(mode, warehouse_id, simulation.get("scenario", "normal"))
        delay = transport_delay_profile(mode, warehouse_id, simulation.get("scenario", "normal"), int(simulation.get("tick", 0)))
        total_lead_hours = lead_hours + int(delay["delay_hours"])
        shipment = {
            "shipment_id": "T" + uuid4().hex[:8],
            "created_tick": simulation.get("tick", 0),
            "created_time": simulation.get("simulation_clock", simulation_time_label(0)),
            "sku": sku,
            "source_warehouse_id": source_id,
            "source_label": source_label,
            "destination_warehouse_id": warehouse_id,
            "destination_label": warehouse_or_pool_label(plan, warehouse_id),
            "quantity": int(quantity),
            "transport_mode": mode,
            "real_lead_time_days": real_lead_time_days(mode, warehouse_id),
            "lead_time_hours": total_lead_hours,
            "base_lead_time_hours": lead_hours,
            "delay_hours": int(delay["delay_hours"]),
            "delay_reason": delay["reason"],
            "eta_confidence": delay["eta_confidence"],
            "remaining_hours": total_lead_hours,
            "status": "在途",
        }
        simulation.setdefault("in_transit_shipments", []).append(shipment)
        active_keys.add((sku, warehouse_id))
        created.append(shipment)
        if len(created) >= 4:
            break
    return created


def process_in_transit_shipments(simulation: dict) -> None:
    active_shipments = []
    arrivals = simulation.setdefault("arrivals", [])
    inventory = simulation.setdefault("inventory", {})
    for shipment in simulation.get("in_transit_shipments", []):
        if shipment.get("status") != "在途":
            continue
        shipment["remaining_hours"] = max(0, int(shipment.get("remaining_hours", 0)) - 1)
        if shipment["remaining_hours"] > 0:
            active_shipments.append(shipment)
            continue
        key = simulation_key(shipment["sku"], shipment["destination_warehouse_id"])
        inventory[key] = int(inventory.get(key, 0)) + int(shipment["quantity"])
        arrival = {
            **shipment,
            "arrived_tick": simulation.get("tick", 0),
            "arrived_time": simulation.get("simulation_clock", simulation_time_label(0)),
            "status": "已到仓",
        }
        arrivals.insert(0, arrival)
        append_simulation_event(
            simulation,
            "到仓入库",
            f"{arrival['arrived_time']} {arrival['destination_label']} 收到 {arrival['quantity']} 件在途补货",
        )
    simulation["in_transit_shipments"] = active_shipments
    del arrivals[SIMULATION_MAX_RECENT_ARRIVALS:]


def active_inbound_by_key(simulation: dict) -> dict[str, int]:
    inbound: dict[str, int] = {}
    for shipment in simulation.get("in_transit_shipments", []):
        if shipment.get("status") != "在途":
            continue
        key = simulation_key(shipment["sku"], shipment["destination_warehouse_id"])
        inbound[key] = inbound.get(key, 0) + int(shipment.get("quantity", 0))
    return inbound


def shipment_source_for(scenario: str, sku: str, warehouse_id: str) -> str:
    scenario = normalize_scenario(scenario)
    if scenario == "eu_rebalance" and warehouse_id == "UK-BHX" and sku.startswith("APCASE"):
        return "DE-FRA"
    if scenario == "us_spike" and warehouse_id == "US-LAX" and sku.startswith("HUB"):
        return "UK-BHX"
    return "GLOBAL-POOL"


def transport_mode_for(scenario: str, sku: str, warehouse_id: str, urgency: float, source_id: str) -> str:
    if source_id not in {"GLOBAL-POOL", "SZ-CENTER"}:
        return "TRANSFER"
    if scenario == "au_delay" and warehouse_id == "AU-SYD" and (sku.startswith("HUB") or sku.startswith("LAPSTAND") or sku.startswith("IPAD")):
        return "AIR"
    if urgency >= 0.75:
        return "AIR"
    return "SEA"


def simulated_lead_time_hours(mode: str, warehouse_id: str, scenario: str) -> int:
    if mode == "TRANSFER":
        return 5
    if mode == "AIR":
        return 8 if not (scenario == "au_delay" and warehouse_id == "AU-SYD") else 10
    if scenario == "au_delay" and warehouse_id == "AU-SYD":
        return 32
    return 24


def transport_delay_profile(mode: str, warehouse_id: str, scenario: str, tick: int) -> dict:
    if mode == "TRANSFER":
        return {"delay_hours": 0, "reason": "跨仓调拨优先排车", "eta_confidence": 0.92}
    if mode == "AIR":
        if scenario == "au_delay" and warehouse_id == "AU-SYD":
            return {"delay_hours": 2, "reason": "澳洲口岸查验排队", "eta_confidence": 0.82}
        return {"delay_hours": 0, "reason": "空运舱位稳定", "eta_confidence": 0.9}
    if scenario == "au_delay" and warehouse_id == "AU-SYD":
        return {"delay_hours": 12, "reason": "海运延误和港口拥堵", "eta_confidence": 0.64}
    if scenario == "black_friday":
        return {"delay_hours": 4 + tick % 4, "reason": "旺季舱位紧张", "eta_confidence": 0.74}
    return {"delay_hours": 1 if tick % 9 == 0 else 0, "reason": "常规清关波动", "eta_confidence": 0.86}


def real_lead_time_days(mode: str, warehouse_id: str) -> int:
    if mode == "TRANSFER":
        return 5
    if mode == "AIR":
        return 7
    return {
        "US-LAX": 25,
        "DE-FRA": 32,
        "UK-BHX": 30,
        "AU-SYD": 28,
    }.get(warehouse_id, 25)


def warehouse_or_pool_label(plan: dict, warehouse_id: str) -> str:
    if warehouse_id in {"GLOBAL-POOL", "SZ-CENTER"}:
        return "深圳中心仓"
    return plan.get("display_names", {}).get("warehouses", {}).get(warehouse_id, warehouse_id)


def append_simulation_event(simulation: dict, event_type: str, message: str) -> None:
    events = simulation.setdefault("events", [])
    if events and events[0].get("message") == message and events[0].get("tick") == simulation.get("tick"):
        return
    events.insert(
        0,
        {
            "event_id": "S" + uuid4().hex[:8],
            "tick": simulation.get("tick", 0),
            "created_at": utc_now(),
            "type": event_type,
            "message": message,
        },
    )
    del events[30:]


def scenario_multiplier(scenario: str, sku: str, warehouse_id: str) -> float:
    scenario = normalize_scenario(scenario)
    multiplier = 1.0
    if scenario == "black_friday":
        multiplier *= 1.65
        if sku.startswith("HUB"):
            multiplier *= 1.25
        if warehouse_id == "US-LAX":
            multiplier *= 1.15
    elif scenario == "us_spike":
        if warehouse_id == "US-LAX":
            multiplier *= 2.25 if sku.startswith("HUB") else 1.75
    elif scenario == "eu_rebalance":
        if warehouse_id == "UK-BHX":
            multiplier *= 1.65 if sku.startswith("APCASE") else 1.25
        if warehouse_id == "DE-FRA":
            multiplier *= 0.72
    elif scenario == "au_delay":
        if warehouse_id == "AU-SYD":
            multiplier *= 1.28
            if sku.startswith("HUB") or sku.startswith("LAPSTAND"):
                multiplier *= 1.2
    return multiplier


def scenario_target_multiplier(scenario: str, warehouse_id: str) -> float:
    if scenario == "au_delay" and warehouse_id == "AU-SYD":
        return 1.28
    if scenario == "black_friday":
        return 1.18
    if scenario == "us_spike" and warehouse_id == "US-LAX":
        return 1.22
    return 1.0


def simulation_risk(days_of_supply: float, stockout_units: int, requested_units: int) -> float:
    shortage = stockout_units / max(1, requested_units)
    if days_of_supply <= 2:
        base = 0.92
    elif days_of_supply <= 5:
        base = 0.72
    elif days_of_supply <= 9:
        base = 0.52
    elif days_of_supply <= 14:
        base = 0.32
    else:
        base = 0.14
    return round(min(0.98, base + shortage * 0.28), 3)


def dynamic_recommendation_reason(scenario: str, warehouse_id: str, gap: int) -> str:
    if gap <= 0:
        return "库存低于 7 天可售水位，建议观察"
    if scenario == "au_delay" and warehouse_id == "AU-SYD":
        return "澳洲物流延迟，动态提高安全库存"
    if scenario == "us_spike" and warehouse_id == "US-LAX":
        return "美国仓爆单，建议优先补货"
    if scenario == "black_friday":
        return "黑五促销拉升需求，建议提前锁定库存"
    if scenario == "eu_rebalance" and warehouse_id == "UK-BHX":
        return "英国仓需求上升，可从欧洲库存池调拨"
    return "实时订单消耗后低于目标库存"


def active_incidents(scenario: str) -> list[dict]:
    scenario = normalize_scenario(scenario)
    if scenario == "black_friday":
        return [{"label": "黑五促销", "impact": "全站需求上升，拓展坞更敏感"}]
    if scenario == "us_spike":
        return [{"label": "美国仓爆单", "impact": "洛杉矶仓 USB-C 拓展坞需求急升"}]
    if scenario == "eu_rebalance":
        return [{"label": "欧洲库存再平衡", "impact": "德国需求放缓，英国耳机保护壳走高"}]
    if scenario == "au_delay":
        return [{"label": "澳洲物流延迟", "impact": "悉尼仓安全库存上调，高毛利 SKU 优先空运"}]
    return [{"label": "常规滚动", "impact": "按历史销量和当前库存连续模拟"}]


def scenario_incident(scenario: str, tick: int) -> dict | None:
    if tick % 6 != 1:
        return None
    scenario = normalize_scenario(scenario)
    messages = {
        "black_friday": ("平台活动", "黑五促销流量进入高峰，系统提高 3C 配件需求倍率"),
        "us_spike": ("爆单", "美国市场 USB-C 拓展坞订单连续放大，洛杉矶仓风险上升"),
        "eu_rebalance": ("调拨", "德国仓需求放缓，英国仓耳机保护壳出现补货压力"),
        "au_delay": ("物流", "深圳到澳洲海运延迟 10 天，悉尼仓安全库存自动上调"),
        "normal": ("滚动", "实时订单继续进入，系统刷新可售天数和补货建议"),
    }
    event_type, message = messages[scenario]
    return {"type": event_type, "message": message}


def estimated_unit_price(sku: str) -> float:
    if sku.startswith("HUB"):
        return 29.99
    if sku.startswith("LAPSTAND"):
        return 18.99
    if sku.startswith("IPAD"):
        return 16.99
    if sku.startswith("STAND"):
        return 9.99
    if sku.startswith("APCASE"):
        return 7.99
    return 6.99


def estimated_unit_margin(plan: dict, sku: str) -> float:
    sku_profile = plan.get("simulation_inputs", {}).get("skus", {}).get(sku, {})
    if "unit_margin" in sku_profile:
        return float(sku_profile["unit_margin"])
    if sku.startswith("HUB"):
        return 11.2
    if sku.startswith("LAPSTAND"):
        return 7.5
    if sku.startswith("IPAD"):
        return 6.3
    if sku.startswith("STAND"):
        return 4.4
    if sku.startswith("APCASE"):
        return 3.2
    return 2.5


class GlobalPoolHandler(BaseHTTPRequestHandler):
    server_version = "GlobalPoolAI/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(render_landing_page())
        elif path == "/login":
            self._send_html(render_login_page())
        elif path == "/logout":
            self._redirect("/", clear_session=True)
        elif path == "/health":
            self._send_json({"status": "ok", "service": "GlobalPool AI"})
        elif path == "/schema":
            self._send_json(schema_summary())
        elif path == "/sample":
            self._send_file(DEFAULT_SAMPLE_PATH, download_name="sample_network.json")
        elif path.startswith("/runs/"):
            self._serve_run_file(path)
        elif path.startswith("/app/runs/"):
            self._serve_run_app(path)
        else:
            self._send_error_page(HTTPStatus.NOT_FOUND, "页面不存在")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/login":
            self._handle_login()
            return
        if path.startswith("/app/runs/"):
            self._handle_run_app_post(path)
            return
        if path != "/simulate":
            self._send_error_page(HTTPStatus.NOT_FOUND, "页面不存在")
            return

        body_length = int(self.headers.get("Content-Length", "0") or 0)
        form = parse_qs(self.rfile.read(body_length).decode("utf-8")) if body_length else {}
        horizon_days = clamp_int((form.get("horizon_days") or ["30"])[0], minimum=7, maximum=90, default=30)
        try:
            result = create_simulation_run(
                runs_dir=self.server.runs_dir,  # type: ignore[attr-defined]
                horizon_days=horizon_days,
            )
        except SimulationDataError as exc:
            self._send_error_page(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        self._send_html(render_result_page(result["metadata"], result["plan"]))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {format % args}")

    def _serve_run_file(self, path: str) -> None:
        match = re.fullmatch(r"/runs/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)", path)
        if not match:
            self._send_error_page(HTTPStatus.BAD_REQUEST, "无效的结果路径。")
            return
        job_id, filename = match.groups()
        if filename not in {"dashboard.html", "replenishment_plan.md", "replenishment_plan.json", "metadata.json", "operations_state.json"}:
            self._send_error_page(HTTPStatus.FORBIDDEN, "不允许访问该文件。")
            return
        self._send_file(self.server.runs_dir / job_id / filename)  # type: ignore[attr-defined]

    def _serve_run_app(self, path: str) -> None:
        if not self._is_authenticated():
            self._redirect(f"/login?next={quote(path, safe='/')}")
            return
        match = re.fullmatch(r"/app/runs/([A-Za-z0-9_-]+)", path)
        if not match:
            self._send_error_page(HTTPStatus.BAD_REQUEST, "无效的控制台路径。")
            return
        job_id = match.group(1)
        try:
            bundle = load_run_bundle(self.server.runs_dir, job_id)  # type: ignore[attr-defined]
        except OperationError as exc:
            self._send_error_page(HTTPStatus.NOT_FOUND, str(exc))
            return
        self._send_html(render_run_app_page(bundle["metadata"], bundle["plan"], bundle["state"]))

    def _handle_run_app_post(self, path: str) -> None:
        if not self._is_authenticated():
            self._redirect(f"/login?next={quote(path, safe='/')}")
            return
        body = self._read_body()
        fields = {key: values[0] for key, values in parse_qs(body.decode("utf-8", errors="replace")).items()}
        approval_match = re.fullmatch(r"/app/runs/([A-Za-z0-9_-]+)/approvals/([A-Za-z0-9_-]+)", path)
        execution_match = re.fullmatch(r"/app/runs/([A-Za-z0-9_-]+)/execution", path)
        try:
            if approval_match:
                job_id, approval_id = approval_match.groups()
                job_dir = self.server.runs_dir / job_id  # type: ignore[attr-defined]
                update_approval_state(
                    job_dir,
                    approval_id=approval_id,
                    decision=fields.get("decision", ""),
                    actor="admin",
                    note=fields.get("note", ""),
                )
                self._redirect(f"/app/runs/{job_id}")
                return
            if execution_match:
                job_id = execution_match.group(1)
                job_dir = self.server.runs_dir / job_id  # type: ignore[attr-defined]
                append_execution_event(
                    job_dir,
                    actor="admin",
                    sku=fields.get("sku", ""),
                    warehouse_id=fields.get("warehouse_id", ""),
                    executed_units=clamp_int(fields.get("executed_units", "0"), 0, 10**9, 0),
                    execution_status=fields.get("execution_status", "已下发"),
                    note=fields.get("note", ""),
                )
                self._redirect(f"/app/runs/{job_id}")
                return
        except OperationError as exc:
            self._send_error_page(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_error_page(HTTPStatus.BAD_REQUEST, "无效的控制台操作。")

    def _handle_login(self) -> None:
        body = self._read_body()
        fields = {key: values[0] for key, values in parse_qs(body.decode("utf-8", errors="replace")).items()}
        password = fields.get("password", "")
        next_path = fields.get("next", "/")
        if hmac.compare_digest(password.encode("utf-8"), admin_password().encode("utf-8")):
            self._redirect(safe_redirect_path(next_path), session_user="admin")
        else:
            self._send_html(render_login_page(error="密码不正确。", next_path=next_path), status=HTTPStatus.UNAUTHORIZED)

    def _read_body(self) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b""
        return self.rfile.read(max(0, content_length))

    def _send_file(self, path: Path, download_name: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send_error_page(HTTPStatus.NOT_FOUND, "文件不存在。")
            return
        content_type = guess_content_type(path)
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_page(self, status: HTTPStatus, message: str) -> None:
        self._send_html(render_error_page(status, message), status=status)

    def _redirect(self, location: str, session_user: str | None = None, clear_session: bool = False) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if session_user:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={sign_session(session_user)}; HttpOnly; SameSite=Lax; Path=/")
        if clear_session:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()

    def _is_authenticated(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and verify_session(value):
                return True
        return False


def clamp_int(raw: str | bytes, minimum: int, maximum: int, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def guess_content_type(path: Path) -> str:
    if path.suffix == ".html":
        return "text/html; charset=utf-8"
    if path.suffix == ".json":
        return "application/json; charset=utf-8"
    if path.suffix == ".md":
        return "text/markdown; charset=utf-8"
    return "application/octet-stream"


def schema_summary() -> dict:
    return {
        "required_top_level_keys": [
            "service_level_z",
            "skus",
            "warehouses",
            "lanes",
            "global_supply_pool",
            "inventory",
            "demand_history",
        ],
        "optional_enhancement_keys": [
            "market_signals",
            "transport_options",
            "warehouse_constraints",
            "procurement_constraints",
        ],
        "sample": "/sample",
        "notes": "仿真引擎使用 data/sample_network.json 结构作为数据源。增强字段缺失时，系统会按默认预测和约束逻辑运行。",
    }


def admin_password() -> str:
    return os.environ.get("GLOBALPOOL_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)


def session_secret() -> str:
    return os.environ.get("GLOBALPOOL_SECRET_KEY", "globalpool-development-secret")


def sign_session(user: str) -> str:
    digest = hmac.new(session_secret().encode("utf-8"), user.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{user}.{digest}"


def verify_session(value: str) -> bool:
    user, separator, digest = value.partition(".")
    if not separator or user != "admin":
        return False
    expected = sign_session(user).partition(".")[2]
    return hmac.compare_digest(digest, expected)


def safe_redirect_path(raw: str) -> str:
    return raw if raw.startswith("/") and not raw.startswith("//") else "/"


def load_run_bundle(runs_dir: Path, job_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise OperationError("无效的任务编号。")
    job_dir = runs_dir / job_id
    if not job_dir.exists() or not job_dir.is_dir():
        raise OperationError("找不到该预测任务。")
    metadata_path = job_dir / "metadata.json"
    plan_path = job_dir / "replenishment_plan.json"
    if not metadata_path.exists() or not plan_path.exists():
        raise OperationError("预测任务文件不完整。")
    return {
        "job_dir": job_dir,
        "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
        "plan": json.loads(plan_path.read_text(encoding="utf-8")),
        "state": load_run_state(job_dir),
    }


def render_landing_page() -> str:
    return page_shell(
        title="GlobalPool AI 海外仓数字孪生仿真",
        body="""
        <section class="hero">
          <div>
            <p class="eyebrow">Overseas Warehouse Digital-Twin Simulation</p>
            <h1>跨境电商海外仓数字孪生仿真系统</h1>
            <p class="lead">仿真引擎实时生成订单流、库存流和在途物流流，构建可控的海外仓运营环境，并在其中验证 AI 销量预测、智能补货与分仓决策。系统以仿真数据为唯一数据源，可独立运行、可演示、可复现。</p>
          </div>
          <div class="status-panel">
            <strong>核心能力</strong>
            <span>实时订单流仿真</span>
            <span>AI 销量预测</span>
            <span>智能库存池分仓</span>
            <span>传统 vs 智能策略对比</span>
            <span>缺货预警与补货审批</span>
          </div>
        </section>
        <section class="upload-panel">
          <form action="/simulate" method="post">
            <label for="horizon_days">预测周期</label>
            <input id="horizon_days" name="horizon_days" type="number" min="7" max="90" value="30">
            <button type="submit">启动仿真实验</button>
          </form>
          <aside>
            <h2>关于数据源</h2>
            <p>本系统不接入企业上传或外部 API，全部数据由仿真引擎生成，确保实验可复现。</p>
            <div class="actions">
              <a href="/sample">查看仿真数据结构</a>
              <a href="/schema">字段说明</a>
            </div>
          </aside>
        </section>
        """,
    )


def render_login_page(error: str = "", next_path: str = "/") -> str:
    message = f'<p class="form-error">{escape(error)}</p>' if error else ""
    return page_shell(
        title="GlobalPool AI 登录",
        body=f"""
        <section class="auth-panel">
          <form action="/login" method="post">
            <input type="hidden" name="next" value="{escape(next_path)}">
            <p class="eyebrow">Operations Console</p>
            <h1>登录运营控制台</h1>
            <label for="password">管理员密码</label>
            <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
            {message}
            <button type="submit">登录</button>
          </form>
        </section>
        """,
    )


def render_result_page(metadata: dict, plan: dict) -> str:
    job_id = metadata["job_id"]
    comparison = plan["simulation_comparison"]
    body = f"""
    <section class="hero compact">
      <div>
        <p class="eyebrow">Simulation Run Complete</p>
        <h1>仿真实验已就绪</h1>
        <p class="lead">运行编号 {escape(job_id)}，已基于仿真引擎生成补货计划、Markdown 报告和 JSON 结果，可进入运营控制台启动实时沙盘。</p>
      </div>
      <div class="status-panel">
        <strong>核心结果</strong>
        <span>补货建议 {metadata['recommendation_count']} 条</span>
        <span>分仓决策 {metadata['allocation_count']} 条</span>
        <span>Fill Rate {comparison['baseline']['fill_rate']} -> {comparison['intelligent']['fill_rate']}</span>
        <span>缺货率 {comparison['baseline']['stockout_rate']} -> {comparison['intelligent']['stockout_rate']}</span>
      </div>
    </section>
    <section class="result-grid">
      <a class="result-card primary" href="/app/runs/{escape(job_id)}">进入运营控制台</a>
      <a class="result-card" href="/runs/{escape(job_id)}/dashboard.html">打开运营看板</a>
      <a class="result-card" href="/runs/{escape(job_id)}/replenishment_plan.md">查看 Markdown 报告</a>
      <a class="result-card" href="/runs/{escape(job_id)}/replenishment_plan.json">下载 JSON 结果</a>
    </section>
    """
    return page_shell("仿真完成", body)


def render_run_app_page(metadata: dict, plan: dict, state: dict) -> str:
    job_id = metadata["job_id"]
    approvals = state.get("approvals", [])
    events = list(reversed(state.get("execution_events", [])))
    allocations = plan.get("allocations", [])
    alerts = plan.get("stockout_alerts", [])
    comparison = plan.get("simulation_comparison", {})
    baseline = comparison.get("baseline", {})
    intelligent = comparison.get("intelligent", {})
    approved_count = sum(1 for row in approvals if row.get("status") == "批准")
    rejected_count = sum(1 for row in approvals if row.get("status") == "驳回")
    pending_count = sum(1 for row in approvals if row.get("status") == "待审批")
    solver = next((row.get("optimizer", "未记录") for row in allocations if row.get("optimizer")), "未记录")
    solver_status = next((row.get("optimizer_status", "未记录") for row in allocations if row.get("optimizer_status")), "未记录")
    visuals = render_console_visuals(plan)

    approval_rows = "\n".join(render_approval_row(job_id, row) for row in approvals)
    if not approval_rows:
        approval_rows = '<tr><td colspan="9" class="muted">当前没有需要人工审批的补货单。</td></tr>'

    event_rows = "\n".join(render_execution_event_row(row) for row in events)
    if not event_rows:
        event_rows = '<tr><td colspan="7" class="muted">尚未回写执行结果。</td></tr>'

    sku_options = render_options(sorted({row.get("sku", "") for row in allocations if row.get("sku")}))
    warehouse_options = render_options(
        sorted({row.get("warehouse_id", "") for row in allocations if row.get("warehouse_id")})
    )

    allocation_rows = "\n".join(
        f"""
        <tr>
          <td>{escape(row.get("sku", ""))}</td>
          <td>{escape(row.get("warehouse_id", ""))}</td>
          <td>{escape(row.get("transport_mode", ""))}</td>
          <td class="num">{fmt_int(row.get("allocated_units", 0))}</td>
          <td class="num">{fmt_int(row.get("unmet_units", 0))}</td>
          <td>{escape(row.get("optimizer", ""))}</td>
          <td>{escape(row.get("capacity_note", ""))}</td>
        </tr>
        """
        for row in allocations
    )

    body = f"""
    <section class="topbar">
      <div>
        <p class="eyebrow">Simulation Run {escape(job_id)}</p>
        <h1>运营控制台</h1>
        <p class="lead">数据源 {escape(metadata.get('data_source', 'simulation-engine'))}，预测周期 {metadata.get('horizon_days', 30)} 天，创建时间 {escape(metadata.get('created_at', ''))}。</p>
      </div>
      <nav class="actions">
        <a href="/runs/{escape(job_id)}/dashboard.html">运营看板</a>
        <a href="/runs/{escape(job_id)}/replenishment_plan.json">JSON</a>
        <a href="/runs/{escape(job_id)}/operations_state.json">执行状态</a>
        <a href="/logout">退出</a>
      </nav>
    </section>

    <section class="metric-grid">
      <div class="metric-card"><span>待审批</span><strong>{pending_count}</strong></div>
      <div class="metric-card"><span>已批准</span><strong>{approved_count}</strong></div>
      <div class="metric-card"><span>已驳回</span><strong>{rejected_count}</strong></div>
      <div class="metric-card"><span>缺货预警</span><strong>{len(alerts)}</strong></div>
      <div class="metric-card"><span>Fill Rate</span><strong>{baseline.get('fill_rate', '-')} -> {intelligent.get('fill_rate', '-')}</strong></div>
      <div class="metric-card"><span>优化器</span><strong>{escape(solver)}</strong><small>{escape(solver_status)}</small></div>
    </section>

    {visuals}

    <section class="ops-grid">
      <div class="ops-panel wide">
        <h2>补货审批</h2>
        <table class="data-table">
          <thead>
            <tr>
              <th>审批单</th><th>SKU</th><th>仓库</th><th class="num">建议</th><th class="num">分配</th><th>运输</th><th class="num">毛利</th><th>状态</th><th>操作</th>
            </tr>
          </thead>
          <tbody>{approval_rows}</tbody>
        </table>
      </div>

      <div class="ops-panel">
        <h2>执行回写</h2>
        <form action="/app/runs/{escape(job_id)}/execution" method="post" class="writeback-form">
          <label>SKU<select name="sku" required>{sku_options}</select></label>
          <label>仓库<select name="warehouse_id" required>{warehouse_options}</select></label>
          <label>执行数量<input name="executed_units" type="number" min="1" step="1" required></label>
          <label>执行状态
            <select name="execution_status">
              <option value="已下发">已下发</option>
              <option value="部分入仓">部分入仓</option>
              <option value="已入仓">已入仓</option>
              <option value="取消">取消</option>
            </select>
          </label>
          <label>备注<input name="note" placeholder="运单、采购单或异常说明"></label>
          <button type="submit">保存执行结果</button>
        </form>
      </div>
    </section>

    <section class="ops-grid">
      <div class="ops-panel wide">
        <h2>执行事件</h2>
        <table class="data-table">
          <thead><tr><th>时间</th><th>事件</th><th>SKU</th><th>仓库</th><th class="num">数量</th><th>状态</th><th>备注</th></tr></thead>
          <tbody>{event_rows}</tbody>
        </table>
      </div>

      <div class="ops-panel">
        <h2>优化分配结果</h2>
        <table class="data-table">
          <thead><tr><th>SKU</th><th>仓库</th><th>运输</th><th class="num">分配</th><th class="num">缺口</th><th>求解器</th><th>约束</th></tr></thead>
          <tbody>{allocation_rows}</tbody>
        </table>
      </div>
    </section>
    """
    return page_shell("运营控制台", body)


def render_console_visuals(plan: dict) -> str:
    allocations = plan.get("allocations", [])
    pool_levels = plan.get("pool_levels", [])
    warehouse_map = plan.get("warehouse_map", [])
    comparison = plan.get("simulation_comparison", {})
    improvement = comparison.get("improvement", {})

    risk_rows = [
        {
            "label": row.get("warehouse_id", ""),
            "value": float(row.get("risk", 0) or 0),
            "display": f"{float(row.get('risk', 0) or 0):.2f}",
            "color": "#b42318" if float(row.get("risk", 0) or 0) >= 0.65 else "#a15c00",
        }
        for row in warehouse_map
    ]
    pool_rows = [
        {
            "label": row.get("sku", ""),
            "value": float(row.get("waterline", 0) or 0),
            "display": f"{round(float(row.get('waterline', 0) or 0) * 100)}%",
            "color": "#b42318" if float(row.get("waterline", 0) or 0) >= 0.8 else "#0f7c8a",
        }
        for row in pool_levels
    ]

    transport_totals: dict[str, float] = {}
    for row in allocations:
        modes = [mode for mode in str(row.get("transport_mode", "")).split("+") if mode and mode != "未分配"]
        if not modes:
            continue
        share = float(row.get("allocated_units", 0) or 0) / len(modes)
        for mode in modes:
            transport_totals[mode] = transport_totals.get(mode, 0.0) + share
    transport_rows = [
        {
            "label": mode,
            "value": value,
            "display": fmt_int(value),
            "color": "#a15c00" if mode == "AIR" else "#167457",
        }
        for mode, value in sorted(transport_totals.items(), key=lambda item: item[1], reverse=True)
    ]

    lift_rows = [
        {"label": "Fill Rate", "value": improvement.get("fill_rate_lift_pct", 0), "display": f"+{improvement.get('fill_rate_lift_pct', 0)}pct", "color": "#167457"},
        {"label": "缺货率下降", "value": improvement.get("stockout_rate_reduction_pct", 0), "display": f"-{improvement.get('stockout_rate_reduction_pct', 0)}pct", "color": "#167457"},
        {"label": "时效缩短", "value": improvement.get("delivery_days_reduction", 0), "display": f"-{improvement.get('delivery_days_reduction', 0)}天", "color": "#0f7c8a"},
        {"label": "库存下降", "value": improvement.get("inventory_reduction_units", 0), "display": fmt_int(improvement.get("inventory_reduction_units", 0)), "color": "#0f7c8a"},
    ]

    return f"""
    <section class="console-viz">
      {render_viz_panel("仓库风险", risk_rows)}
      {render_viz_panel("库存池水位", pool_rows)}
      {render_viz_panel("运输方式", transport_rows)}
      {render_viz_panel("仿真改善", lift_rows)}
    </section>
    """


def render_viz_panel(title: str, rows: list[dict]) -> str:
    if not rows:
        bars = '<p class="muted">暂无可视化数据。</p>'
    else:
        max_value = max(1.0, max(abs(float(row.get("value", 0) or 0)) for row in rows))
        bars = "\n".join(render_viz_bar(row, max_value) for row in rows[:6])
    return f"""
    <div class="viz-panel">
      <h2>{escape(title)}</h2>
      <div class="viz-bars">{bars}</div>
    </div>
    """


def render_viz_bar(row: dict, max_value: float) -> str:
    value = abs(float(row.get("value", 0) or 0))
    width = max(3, min(100, round(value / max_value * 100)))
    color = row.get("color", "#2358d8")
    return f"""
    <div class="viz-row">
      <span class="viz-label">{escape(str(row.get("label", "")))}</span>
      <span class="viz-track"><i style="width:{width}%; background:{escape(str(color))}"></i></span>
      <span class="viz-value">{escape(str(row.get("display", fmt_int(value))))}</span>
    </div>
    """


def render_approval_row(job_id: str, row: dict) -> str:
    approval_id = row.get("approval_id", "")
    status = row.get("status", "待审批")
    return f"""
    <tr>
      <td>{escape(approval_id)}</td>
      <td>{escape(row.get("sku", ""))}</td>
      <td>{escape(row.get("warehouse_id", ""))}</td>
      <td class="num">{fmt_int(row.get("recommended_units", 0))}</td>
      <td class="num">{fmt_int(row.get("allocated_units", 0))}</td>
      <td>{escape(row.get("transport_mode", ""))}</td>
      <td class="num">{fmt_money(row.get("estimated_margin", 0))}</td>
      <td><span class="status {status_class(status)}">{escape(status)}</span></td>
      <td>
        <form action="/app/runs/{escape(job_id)}/approvals/{escape(approval_id)}" method="post" class="decision-form">
          <input name="note" value="{escape(row.get("note", ""))}" placeholder="备注">
          <div class="decision-actions">
            <button name="decision" value="批准" type="submit" class="success">批准</button>
            <button name="decision" value="驳回" type="submit" class="danger">驳回</button>
          </div>
        </form>
      </td>
    </tr>
    """


def render_execution_event_row(row: dict) -> str:
    return f"""
    <tr>
      <td>{escape(row.get("created_at", ""))}</td>
      <td>{escape(row.get("event_id", ""))}</td>
      <td>{escape(row.get("sku", ""))}</td>
      <td>{escape(row.get("warehouse_id", ""))}</td>
      <td class="num">{fmt_int(row.get("executed_units", 0))}</td>
      <td>{escape(row.get("execution_status", ""))}</td>
      <td>{escape(row.get("note", ""))}</td>
    </tr>
    """


def render_options(values: list[str]) -> str:
    if not values:
        return '<option value="">无可选项</option>'
    return "\n".join(f'<option value="{escape(value)}">{escape(value)}</option>' for value in values)


def status_class(status: str) -> str:
    return {"批准": "approved", "驳回": "rejected"}.get(status, "pending")


def fmt_int(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_money(value: object) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def render_error_page(status: HTTPStatus, message: str) -> str:
    return page_shell(
        title=f"{status.value} {status.phrase}",
        body=f"""
        <section class="hero compact">
          <div>
            <p class="eyebrow">Error</p>
            <h1>{status.value} {escape(status.phrase)}</h1>
            <p class="lead">{escape(message)}</p>
          </div>
        </section>
        <section class="result-grid"><a class="result-card primary" href="/">返回首页</a></section>
        """,
    )


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --ink:#17212f; --muted:#64748b; --line:#d7dee8; --panel:#fff; --bg:#f5f7fa;
      --blue:#2358d8; --cyan:#0f7c8a; --green:#167457; --warn:#a15c00;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px; }}
    .hero {{ display:grid; grid-template-columns:1.25fr .75fr; gap:18px; align-items:stretch; margin-bottom:18px; }}
    .hero.compact {{ grid-template-columns:1fr .85fr; }}
    .hero > div, .upload-panel, .result-card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; }}
    .eyebrow {{ color:var(--cyan); font-weight:800; margin:0 0 8px; text-transform:uppercase; letter-spacing:.04em; font-size:12px; }}
    h1 {{ margin:0; font-size:34px; line-height:1.12; letter-spacing:0; }}
    h2 {{ margin:0 0 8px; font-size:18px; }}
    .lead {{ color:var(--muted); margin:12px 0 0; max-width:760px; }}
    .status-panel {{ display:grid; gap:8px; }}
    .status-panel strong {{ font-size:18px; }}
    .status-panel span {{ display:block; padding:9px 10px; background:#f0f5ff; border:1px solid #d8e5ff; border-radius:6px; font-weight:650; }}
    .upload-panel {{ display:grid; grid-template-columns:1fr .7fr; gap:20px; }}
    form {{ display:grid; gap:12px; }}
    label {{ font-weight:750; }}
    input, select {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:11px; background:#fbfdff; font:inherit; }}
    button, .actions a, .result-card {{ border:0; background:var(--blue); color:#fff; border-radius:6px; padding:12px 14px; font:inherit; font-weight:800; text-decoration:none; cursor:pointer; display:inline-flex; justify-content:center; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
    .actions a {{ background:#eef4ff; color:var(--blue); border:1px solid #d6e4ff; }}
    .result-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
    .result-card {{ min-height:104px; align-items:center; color:var(--ink); background:#fff; border:1px solid var(--line); text-align:center; }}
    .result-card.primary {{ background:var(--blue); color:#fff; }}
    .auth-panel {{ max-width:430px; margin:8vh auto 0; background:#fff; border:1px solid var(--line); border-radius:8px; padding:24px; }}
    .auth-panel h1 {{ margin-bottom:18px; }}
    .form-error {{ color:#b42318; font-weight:750; margin:0; }}
    .topbar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:16px; background:#fff; border:1px solid var(--line); border-radius:8px; padding:20px; }}
    .topbar .actions {{ margin-top:0; justify-content:flex-end; }}
    .metric-grid {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-bottom:14px; }}
    .metric-card {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:13px; min-height:86px; }}
    .metric-card span, .metric-card small {{ display:block; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }}
    .metric-card strong {{ display:block; margin-top:4px; font-size:21px; line-height:1.18; overflow-wrap:anywhere; }}
    .console-viz {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:14px; }}
    .viz-panel {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; min-height:178px; }}
    .viz-panel h2 {{ margin-bottom:12px; }}
    .viz-bars {{ display:grid; gap:9px; }}
    .viz-row {{ display:grid; grid-template-columns:76px 1fr 54px; gap:8px; align-items:center; }}
    .viz-label {{ font-weight:750; overflow-wrap:anywhere; }}
    .viz-track {{ height:11px; background:#edf2f7; border-radius:999px; overflow:hidden; }}
    .viz-track i {{ display:block; height:100%; border-radius:inherit; }}
    .viz-value {{ color:var(--muted); font-size:12px; text-align:right; white-space:nowrap; }}
    .ops-grid {{ display:grid; grid-template-columns:1.45fr .85fr; gap:14px; align-items:start; margin-bottom:14px; }}
    .ops-panel {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:16px; overflow:auto; }}
    .ops-panel.wide {{ min-width:0; }}
    .data-table {{ width:100%; border-collapse:collapse; table-layout:auto; }}
    .data-table th, .data-table td {{ border-bottom:1px solid var(--line); padding:9px 8px; text-align:left; vertical-align:top; }}
    .data-table th {{ color:var(--muted); font-size:12px; font-weight:800; white-space:nowrap; }}
    .data-table td {{ overflow-wrap:anywhere; }}
    .data-table .num {{ text-align:right; white-space:nowrap; }}
    .muted {{ color:var(--muted); }}
    .status {{ display:inline-flex; border-radius:999px; padding:3px 8px; font-weight:800; font-size:12px; white-space:nowrap; }}
    .status.pending {{ background:#fff7e6; color:var(--warn); }}
    .status.approved {{ background:#e9f7f3; color:var(--green); }}
    .status.rejected {{ background:#fff1f0; color:#b42318; }}
    .decision-form {{ min-width:190px; gap:7px; }}
    .decision-form input {{ padding:8px; }}
    .decision-actions {{ display:grid; grid-template-columns:1fr 1fr; gap:7px; }}
    .decision-actions button, .writeback-form button {{ padding:9px 10px; }}
    button.success {{ background:var(--green); }}
    button.danger {{ background:#b42318; }}
    .writeback-form {{ display:grid; grid-template-columns:1fr; gap:10px; }}
    .writeback-form label {{ display:grid; gap:5px; }}
    @media (max-width:1040px) {{ .metric-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .console-viz {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .ops-grid {{ grid-template-columns:1fr; }} }}
    @media (max-width:860px) {{ main {{ padding:16px; }} .hero, .hero.compact, .upload-panel, .result-grid, .metric-grid, .console-viz {{ grid-template-columns:1fr; }} .topbar {{ display:grid; }} .viz-row {{ grid-template-columns:1fr; }} .viz-value {{ text-align:left; }} h1 {{ font-size:26px; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""


def run_server(host: str, port: int, runs_dir: Path) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), GlobalPoolHandler)
    server.runs_dir = runs_dir  # type: ignore[attr-defined]
    print(f"GlobalPool AI web app running at http://{host}:{port}")
    print(f"Prediction runs directory: {runs_dir}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GlobalPool AI overseas-warehouse digital-twin simulation web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()
    run_server(args.host, args.port, args.runs_dir)


if __name__ == "__main__":
    main()
