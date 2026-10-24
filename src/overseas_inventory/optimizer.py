from __future__ import annotations

from dataclasses import dataclass
from math import floor

from .models import Network, TransportOption
from .replenishment import ReplenishmentRecommendation


@dataclass(frozen=True)
class OptimizedShipment:
    sku: str
    warehouse_id: str
    transport_mode: str
    units: int
    lead_time_days: int
    unit_cost: float


@dataclass(frozen=True)
class OptimizationResult:
    shipments: list[OptimizedShipment]
    status: str
    objective_value: float | None
    solver: str


def optimize_shipments_milp(
    network: Network,
    recommendations: list[ReplenishmentRecommendation],
    priority_scores: dict[tuple[str, str], float],
) -> OptimizationResult:
    """Allocate inventory with SciPy MILP, falling back to an empty infeasible result on solver failure."""
    try:
        from scipy.optimize import Bounds, LinearConstraint, milp
    except Exception:  # pragma: no cover - depends on deployment image.
        return OptimizationResult(shipments=[], status="scipy-milp-unavailable", objective_value=None, solver="fallback")

    active_recommendations = [item for item in recommendations if item.recommended_units > 0]
    variables: list[tuple[ReplenishmentRecommendation, TransportOption]] = []
    objective: list[float] = []
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []

    for item in active_recommendations:
        sku = network.skus[item.sku]
        options = [
            option
            for option in network.transport_options
            if option.source == "GLOBAL-POOL" and option.target == item.warehouse_id
        ]
        if not options:
            fallback = network.transport_option(item.warehouse_id, item.urgency)
            options = [fallback] if fallback else []
        for option in options:
            max_packs = min(floor(option.max_units / sku.pack_size), floor(item.recommended_units / sku.pack_size))
            if max_packs <= 0:
                continue
            variables.append((item, option))
            priority = priority_scores[(item.sku, item.warehouse_id)]
            speed_bonus = max(0.0, 30 - option.lead_time_days) * 0.012
            reliability_bonus = option.reliability * 0.18
            cost_penalty = option.unit_cost / max(1.0, sku.unit_margin) * 0.08
            score = priority + speed_bonus + reliability_bonus - cost_penalty
            objective.append(-score * sku.pack_size)
            lower_bounds.append(0)
            upper_bounds.append(max_packs)

    if not variables:
        return OptimizationResult(shipments=[], status="no-milp-variables", objective_value=None, solver="scipy.optimize.milp")

    constraints = []
    lb = []
    ub = []

    for sku_id, warehouse_id in sorted({(item.sku, item.warehouse_id) for item in active_recommendations}):
        item = next(row for row in active_recommendations if row.sku == sku_id and row.warehouse_id == warehouse_id)
        row = [0.0] * len(variables)
        for index, (var_item, _) in enumerate(variables):
            if var_item.sku == sku_id and var_item.warehouse_id == warehouse_id:
                row[index] = network.skus[sku_id].pack_size
        constraints.append(row)
        lb.append(0)
        ub.append(item.recommended_units)

    for sku_id in sorted({item.sku for item in active_recommendations}):
        sku = network.skus[sku_id]
        pool_supply = network.global_supply_pool.get(sku_id)
        pool_units = pool_supply.allocatable_units if pool_supply else 0
        procurement = network.procurement_constraints.get(sku_id)
        max_procurement_units = procurement.available_units if procurement else pool_units
        capital_budget = procurement.capital_budget if procurement else 10**12
        purchase_cost = procurement.unit_purchase_cost if procurement else 0.0

        pool_row = [sku.pack_size if var_item.sku == sku_id else 0.0 for var_item, _ in variables]
        constraints.append(pool_row)
        lb.append(0)
        ub.append(min(pool_units, max_procurement_units))

        capital_row = [sku.pack_size * purchase_cost if var_item.sku == sku_id else 0.0 for var_item, _ in variables]
        constraints.append(capital_row)
        lb.append(0)
        ub.append(capital_budget)

    for warehouse_id in sorted({item.warehouse_id for item in active_recommendations}):
        capacity = _available_warehouse_capacity(network, warehouse_id)
        row = [
            network.skus[var_item.sku].pack_size if var_item.warehouse_id == warehouse_id else 0.0
            for var_item, _ in variables
        ]
        constraints.append(row)
        lb.append(0)
        ub.append(capacity)

    try:
        result = milp(
            c=objective,
            integrality=[1] * len(variables),
            bounds=Bounds(lower_bounds, upper_bounds),
            constraints=LinearConstraint(constraints, lb, ub),
            options={"time_limit": 5.0, "mip_rel_gap": 0.02},
        )
    except Exception as exc:  # pragma: no cover - solver/environment dependent.
        return OptimizationResult(
            shipments=[],
            status=f"milp-error:{exc}",
            objective_value=None,
            solver="scipy.optimize.milp",
        )

    shipments: list[OptimizedShipment] = []
    if result.success and result.x is not None:
        for value, (item, option) in zip(result.x, variables):
            sku = network.skus[item.sku]
            packs = int(round(max(0, value)))
            units = packs * sku.pack_size
            if units <= 0:
                continue
            shipments.append(
                OptimizedShipment(
                    sku=item.sku,
                    warehouse_id=item.warehouse_id,
                    transport_mode=option.mode,
                    units=units,
                    lead_time_days=option.lead_time_days,
                    unit_cost=option.unit_cost,
                )
            )

    status = "optimal" if shipments else str(result.message)
    objective_value = round(float(result.fun), 4) if result.fun is not None else None
    return OptimizationResult(
        shipments=shipments,
        status=status,
        objective_value=objective_value,
        solver="scipy.optimize.milp",
    )


def _available_warehouse_capacity(network: Network, warehouse_id: str) -> int:
    constraint = network.warehouse_constraints.get(warehouse_id)
    if constraint is None:
        return 10**9
    current_position = sum(
        item.available_position for item in network.inventory.values() if item.warehouse_id == warehouse_id
    )
    return max(0, min(constraint.max_daily_inbound_units, constraint.capacity_units - current_position))
