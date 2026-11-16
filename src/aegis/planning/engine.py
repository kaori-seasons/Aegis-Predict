"""Pure planning kernel: frozen network + policy -> forecasts, cost MILP, explainable lines.

The money objective is dispatch cost plus unmet-target penalties. It is a planning surrogate,
not a claim to model order-level service or full container loading. All quantities are base units.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from math import ceil, isfinite, sqrt
from statistics import fmean
from typing import Any

from overseas_inventory.models import Network, TransportOption, network_from_payload
from .forecast import MODEL_VERSION, forecast
from .policy import PlanningPolicy

ALGORITHM_VERSION = "aegis-cost-milp-v2"


def validate_network(network: Network) -> None:
    seen = set()
    for sku in network.skus.values():
        if sku.pack_size <= 0 or not isfinite(sku.unit_margin) or sku.unit_margin < 0:
            raise ValueError(f"invalid SKU economics: {sku.sku}")
    for lane in network.lanes:
        if min(lane.lead_time_days, lane.lead_time_std_days, lane.unit_cost) < 0:
            raise ValueError("negative lane values")
    for series in network.demand_history:
        key = (series.sku, series.warehouse_id)
        if key in seen or series.sku not in network.skus or series.warehouse_id not in network.warehouses:
            raise ValueError(f"duplicate or unknown demand key: {key}")
        if key not in network.inventory or network.lane_from_pool(series.warehouse_id) is None:
            raise ValueError(f"missing inventory or replenishment lane: {key}")
        seen.add(key)
    if not seen:
        raise ValueError("no demand series to plan")
    for inv in network.inventory.values():
        if min(inv.on_hand, inv.in_transit, inv.allocated, inv.on_order, inv.backorder) < 0:
            raise ValueError("negative inventory components")
    for option in network.transport_options:
        if min(option.max_units, option.unit_cost, option.lead_time_days) < 0 or not 0 <= option.reliability <= 1:
            raise ValueError("invalid transport option")


def _capacity(network: Network, warehouse: str) -> int:
    constraint = network.warehouse_constraints.get(warehouse)
    if constraint is None:
        return max(0, network.warehouses[warehouse].handling_capacity_per_day)
    physical = sum(i.on_hand + i.in_transit + i.on_order for i in network.inventory.values() if i.warehouse_id == warehouse)
    return max(0, min(constraint.capacity_units - physical, constraint.max_daily_inbound_units))


def _options(network: Network, warehouse: str) -> list[TransportOption]:
    options = [o for o in network.transport_options if o.source == "GLOBAL-POOL" and o.target == warehouse]
    if not options:
        lane = network.lane_from_pool(warehouse)
        options = [TransportOption("GLOBAL-POOL", warehouse, "STANDARD", lane.lead_time_days, lane.unit_cost, 1.0, 10**9)]
    return sorted(options, key=lambda o: o.mode)


def solve(payload: dict, policy: PlanningPolicy, horizon_days: int = 30, *, seed: int = 42, pins: list[dict] | None = None) -> dict:
    network = network_from_payload(payload)
    validate_network(network)
    if horizon_days < 1 or horizon_days > 365:
        raise ValueError("horizon_days must be between 1 and 365")
    categories: dict[str, list[float]] = defaultdict(list)
    for s in network.demand_history:
        flags = s.stockout_days or [False] * len(s.daily_units)
        missing = s.missing_days or [False] * len(s.daily_units)
        flags = [c or m for c, m in zip(flags, missing)]
        observed = [v for v, censored in zip(s.daily_units, flags) if not censored]
        if len(observed) >= 7:
            categories[network.skus[s.sku].category].append(fmean(observed[-28:]))
    lines = []
    for series in sorted(network.demand_history, key=lambda s: (s.sku, s.warehouse_id)):
        sku = network.skus[series.sku]
        rates = categories[sku.category]
        fc = forecast(series, horizon_days, cold_start_rate=fmean(rates) if rates else 0.0)
        lane = network.lane_from_pool(series.warehouse_id)
        inv = network.inventory[(series.sku, series.warehouse_id)]
        z = policy.service_level_z.get(sku.service_class, 1.28)
        if not isfinite(z) or z < 0 or z > 5:
            raise ValueError("service level z must be finite and within [0, 5]")
        def safety(days: int) -> float:
            return z * sqrt(days * fc.volatility**2 + fc.daily_rate**2 * lane.lead_time_std_days**2)
        protection = lane.lead_time_days + policy.review_period_days
        reorder = ceil(fc.daily_rate * lane.lead_time_days + safety(lane.lead_time_days))
        target = ceil(fc.daily_rate * protection + safety(protection))
        need = max(0, ceil((target - inv.available_position) / sku.pack_size) * sku.pack_size)
        urgency = min(1.0, max(0, (reorder - inv.available_position) / max(1, reorder)))
        lines.append({"sku": series.sku, "warehouse_id": series.warehouse_id, "forecast": fc.to_dict(),
                      "daily_rate": fc.daily_rate, "inventory_position": inv.available_position,
                      "safety_stock": ceil(safety(protection)), "reorder_point": reorder, "target_stock": target,
                      "recommended_units": need, "urgency": round(urgency, 6), "pinned": False,
                      "approval_required": need * sku.unit_margin >= policy.approval_value_threshold or
                      urgency >= policy.approval_urgency_threshold or fc.confidence < policy.min_confidence,
                      "reason": "库存不足，补足提前期与复盘周期需求及安全库存" if need else "库存覆盖保护周期"})
    by_key = {(r["sku"], r["warehouse_id"]): r for r in lines}
    pin_map = {}
    for pin in pins or []:
        key = (pin["sku"], pin["warehouse_id"])
        if key not in by_key or key in pin_map:
            raise ValueError(f"unknown or duplicate pinned line: {key}")
        row = by_key[key]
        if pin["units"] < 0 or pin["units"] % network.skus[key[0]].pack_size:
            raise ValueError("pinned quantity must be nonnegative whole packs")
        row["recommended_units"] = max(row["recommended_units"], pin["units"])
        row["pinned"] = True
        pin_map[key] = pin
    shipments, diagnostics = _optimize(network, lines, policy, pin_map)
    for row in lines:
        key = (row["sku"], row["warehouse_id"])
        row["shipments"] = [s for s in shipments if (s["sku"], s["warehouse_id"]) == key]
        row["allocated_units"] = sum(s["units"] for s in row["shipments"])
        row["final_units"] = row["allocated_units"]
        row["unmet_units"] = row["recommended_units"] - row["allocated_units"]
        row["transport_mode"] = "+".join(s["transport_mode"] for s in row["shipments"])
        row["lead_time_days"] = max((s["lead_time_days"] for s in row["shipments"]), default=0)
    dispatch_cost = sum(s["units"] * s["unit_total_cost"] for s in shipments)
    penalty = sum(r["unmet_units"] * _penalty(r, policy) for r in lines)
    return {"algorithm_version": ALGORITHM_VERSION, "model_version": MODEL_VERSION, "seed": seed,
            "lines": lines, "optimizer": diagnostics,
            "summary": {"recommended_units": sum(r["recommended_units"] for r in lines),
                        "allocated_units": sum(r["allocated_units"] for r in lines),
                        "unmet_units": sum(r["unmet_units"] for r in lines),
                        "dispatch_cost": round(dispatch_cost, 4), "shortage_penalty": round(penalty, 4),
                        "total_cost": round(dispatch_cost + penalty, 4)}}


def _penalty(row: dict, policy: PlanningPolicy) -> float:
    return policy.stockout_penalty * (1 + row["urgency"])


def _optimize(network: Network, lines: list[dict], policy: PlanningPolicy, pins: dict) -> tuple[list[dict], dict]:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    variables: list[tuple[dict, TransportOption, int, float]] = []
    cost, upper = [], []
    for row in lines:
        pack = network.skus[row["sku"]].pack_size
        pin = pins.get((row["sku"], row["warehouse_id"]))
        allowed = _options(network, row["warehouse_id"])
        if pin and pin.get("transport_mode") and pin["transport_mode"] not in {o.mode for o in allowed}:
            raise ValueError("pinned transport mode is unavailable")
        for option in allowed:
            if pin and pin.get("transport_mode") and option.mode != pin["transport_mode"]:
                continue
            if row["recommended_units"] == 0:
                continue
            wc = network.warehouse_constraints.get(row["warehouse_id"])
            holding = wc.storage_cost_per_unit_day if wc else policy.holding_cost_per_unit_day
            # Expected late-demand loss is capped by daily rate and shipment quantity.
            inv = network.inventory[(row["sku"], row["warehouse_id"])]
            available_now = max(0, inv.on_hand - inv.allocated - inv.backorder)
            cover_days = available_now / max(row["daily_rate"], 1e-9)
            late_days = max(0.0, option.lead_time_days - cover_days)
            delay_cost = policy.stockout_penalty * (
                1 - option.reliability + late_days / max(1, option.lead_time_days + policy.review_period_days)
            )
            unit_cost = option.unit_cost + holding * policy.review_period_days / 2 + policy.duty_per_unit + policy.last_mile_per_unit + delay_cost
            variables.append((row, option, pack, unit_cost))
            cost.append((unit_cost - _penalty(row, policy)) * pack)
            upper.append(min(row["recommended_units"], option.max_units) // pack)
    if not variables:
        if any(p["units"] > 0 for p in pins.values()):
            raise ValueError("pinned plan infeasible")
        return [], {"solver": "scipy.optimize.milp", "status": "no-demand", "mip_gap": 0.0}
    n = len(variables)
    rows: list[dict[int, float]] = []
    lbs, ubs = [], []
    def constraint(coeff: dict[int, float], lb: float, ub: float) -> None:
        rows.append(coeff); lbs.append(lb); ubs.append(ub)
    for row in lines:
        key = (row["sku"], row["warehouse_id"])
        coeff = {i: v[2] for i, v in enumerate(variables) if (v[0]["sku"], v[0]["warehouse_id"]) == key}
        pin = pins.get(key)
        constraint(coeff, pin["units"] if pin else 0, pin["units"] if pin else row["recommended_units"])
    for sku in sorted(network.skus):
        supply = network.global_supply_pool.get(sku)
        limit = supply.allocatable_units if supply else 0
        terms = network.procurement_constraints.get(sku)
        coeff = {i: v[2] for i, v in enumerate(variables) if v[0]["sku"] == sku}
        if terms:
            limit = min(limit, terms.available_units)
            if terms.unit_purchase_cost > 0:
                limit = min(limit, int(terms.capital_budget / terms.unit_purchase_cost))
        constraint(coeff, 0, limit)
        if terms and terms.min_order_qty > 0 and coeff:
            # Binary activation enforces either zero dispatch or the supplier minimum.
            j = len(cost)
            cost.append(0.0); upper.append(1)
            constraint({**coeff, j: -limit}, float("-inf"), 0)
            constraint({**coeff, j: -terms.min_order_qty}, 0, float("inf"))
    for wh in sorted(network.warehouses):
        constraint({i: v[2] for i, v in enumerate(variables) if v[0]["warehouse_id"] == wh}, 0, _capacity(network, wh))
        # Transport max_units is a shared route quota across all SKUs.
        for option in _options(network, wh):
            constraint({i: v[2] for i, v in enumerate(variables) if v[1] == option}, 0, option.max_units)
    ri, ci, data = [], [], []
    for r, coeff in enumerate(rows):
        for c, value in coeff.items():
            ri.append(r); ci.append(c); data.append(value)
    matrix = coo_matrix((data, (ri, ci)), shape=(len(rows), len(cost))).tocsc()
    def feasible(x: Any) -> bool:
        if x is None or not np.all(np.isfinite(x)):
            return False
        candidate = np.round(x)
        totals = matrix @ candidate
        return bool(np.all(np.abs(candidate-x) < 1e-5) and np.all(candidate >= 0) and
                    np.all(candidate <= np.array(upper) + 1e-5) and np.all(totals >= np.array(lbs)-1e-5) and
                    np.all(totals <= np.array(ubs)+1e-5))
    result = None
    try:
        result = milp(np.array(cost), integrality=np.ones(len(cost)), bounds=Bounds(np.zeros(len(cost)), upper),
                      constraints=LinearConstraint(matrix, lbs, ubs),
                      options={"time_limit": policy.solver_time_limit_seconds, "mip_rel_gap": policy.solver_mip_gap})
    except (ValueError, RuntimeError):
        pass
    if result is not None and feasible(result.x):
        x = np.round(result.x)
        status = "optimal" if result.status == 0 else "feasible-time-limit"
    else:
        # Deterministic bounded greedy fallback; same hard-constraint matrix, including pins/MOQ.
        x = np.zeros(len(cost))
        if pins:
            raise ValueError("solver could not satisfy pinned plan; manual constraints were preserved")
        for i in sorted(range(n), key=lambda i: (cost[i] / variables[i][2], i)):
            if cost[i] >= 0:
                continue
            sku = variables[i][0]["sku"]
            terms = network.procurement_constraints.get(sku)
            # Avoid an unsafe partial MOQ allocation in fallback; leave unmet instead.
            if terms and terms.min_order_qty:
                continue
            maximum = int(upper[i])
            for coeff, ub in zip(rows, ubs):
                if i in coeff and isfinite(ub):
                    used = sum(v * x[j] for j, v in coeff.items())
                    maximum = min(maximum, max(0, int((ub-used+1e-6) / coeff[i])))
            x[i] = maximum
        if not feasible(x):
            raise ValueError("no feasible fallback allocation")
        status = "heuristic-fallback"
    shipments = []
    for i, (row, option, pack, unit_cost) in enumerate(variables):
        units = int(x[i]) * pack
        if units:
            shipments.append({"sku": row["sku"], "warehouse_id": row["warehouse_id"], "transport_mode": option.mode,
                              "units": units, "lead_time_days": option.lead_time_days, "unit_cost": option.unit_cost,
                              "unit_total_cost": unit_cost})
    return shipments, {"solver": "scipy.optimize.milp", "status": status,
                       "mip_gap": float(result.mip_gap) if result is not None and getattr(result, "mip_gap", None) is not None else None}
