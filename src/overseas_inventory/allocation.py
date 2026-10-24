from __future__ import annotations

from dataclasses import dataclass
from math import floor

from .models import Network
from .optimizer import optimize_shipments_milp
from .replenishment import ReplenishmentRecommendation


@dataclass(frozen=True)
class AllocationDecision:
    sku: str
    warehouse_id: str
    source: str
    transport_mode: str
    allocated_units: int
    unmet_units: int
    lead_time_days: int
    priority_score: float
    approval_status: str
    capacity_note: str
    optimizer: str
    optimizer_status: str
    decision_note: str


def _floor_to_pack(quantity: int, pack_size: int) -> int:
    if quantity <= 0:
        return 0
    return floor(quantity / pack_size) * pack_size


def _priority_score(network: Network, recommendation: ReplenishmentRecommendation) -> float:
    sku = network.skus[recommendation.sku]
    warehouse = network.warehouses[recommendation.warehouse_id]
    lane = network.lane_from_pool(recommendation.warehouse_id)
    service_weight = 1.18 if sku.service_class == "premium" else 1.0
    margin_after_lane = max(0.1, sku.unit_margin - (lane.unit_cost if lane else 0))
    margin_score = min(1.0, margin_after_lane / max(1.0, sku.unit_margin))
    need_score = min(1.0, recommendation.recommended_units / max(1, recommendation.target_stock))

    score = (
        recommendation.urgency * 0.48
        + need_score * 0.22
        + warehouse.last_mile_score * 0.16
        + margin_score * 0.14
    ) * service_weight
    return round(score, 4)


def allocate_from_global_pool(
    network: Network,
    recommendations: list[ReplenishmentRecommendation],
) -> list[AllocationDecision]:
    priority_scores = {
        (item.sku, item.warehouse_id): _priority_score(network, item)
        for item in recommendations
        if item.recommended_units > 0
    }
    optimized = optimize_shipments_milp(network, recommendations, priority_scores)
    if optimized.shipments:
        return _decisions_from_optimized_shipments(network, recommendations, priority_scores, optimized)
    return _allocate_with_heuristic(network, recommendations, priority_scores, optimized.solver, optimized.status)


def _allocate_with_heuristic(
    network: Network,
    recommendations: list[ReplenishmentRecommendation],
    priority_scores: dict[tuple[str, str], float],
    solver: str,
    solver_status: str,
) -> list[AllocationDecision]:
    decisions: list[AllocationDecision] = []
    allocated_by_warehouse: dict[str, int] = {}

    for sku_id in sorted({item.sku for item in recommendations}):
        sku_recommendations = [item for item in recommendations if item.sku == sku_id and item.recommended_units > 0]
        if not sku_recommendations:
            continue

        sku = network.skus[sku_id]
        pool_supply = network.global_supply_pool.get(sku_id)
        remaining_pool = pool_supply.allocatable_units if pool_supply else 0
        scored = sorted(
            ((priority_scores[(item.sku, item.warehouse_id)], item) for item in sku_recommendations),
            key=lambda pair: pair[0],
            reverse=True,
        )
        total_weight = sum(max(0.01, score) * item.recommended_units for score, item in scored)
        provisional: dict[tuple[str, str], int] = {}

        for score, item in scored:
            if remaining_pool <= 0 or total_weight <= 0:
                provisional[(item.sku, item.warehouse_id)] = 0
                continue
            share = round(remaining_pool * ((max(0.01, score) * item.recommended_units) / total_weight))
            provisional[(item.sku, item.warehouse_id)] = min(
                item.recommended_units,
                _floor_to_pack(share, sku.pack_size),
            )

        allocated_total = sum(provisional.values())
        remaining_after_provisional = max(0, remaining_pool - allocated_total)

        for score, item in scored:
            key = (item.sku, item.warehouse_id)
            available_capacity = _available_warehouse_capacity(network, item.warehouse_id, allocated_by_warehouse)
            while (
                remaining_after_provisional >= sku.pack_size
                and provisional[key] + sku.pack_size <= item.recommended_units
                and provisional[key] + sku.pack_size <= available_capacity
            ):
                provisional[key] += sku.pack_size
                remaining_after_provisional -= sku.pack_size

        for score, item in scored:
            option = network.transport_option(item.warehouse_id, item.urgency)
            lane = network.lane_from_pool(item.warehouse_id)
            allocated = provisional[(item.sku, item.warehouse_id)]
            available_capacity = _available_warehouse_capacity(network, item.warehouse_id, allocated_by_warehouse)
            if allocated > available_capacity:
                allocated = _floor_to_pack(available_capacity, sku.pack_size)
            allocated_by_warehouse[item.warehouse_id] = allocated_by_warehouse.get(item.warehouse_id, 0) + allocated
            unmet = max(0, item.recommended_units - allocated)
            if allocated == 0 and unmet > 0:
                note = "库存池可分配量不足，列入采购或跨仓调拨队列"
            elif unmet > 0:
                note = "库存池部分满足，缺口进入下一轮采购与调拨"
            else:
                note = "库存池可满足本轮补货建议"
            capacity_note = _capacity_note(network, item.warehouse_id, allocated_by_warehouse[item.warehouse_id])
            approval_status = "待审批" if item.approval_required else "自动通过"

            decisions.append(
                AllocationDecision(
                    sku=item.sku,
                    warehouse_id=item.warehouse_id,
                    source="GLOBAL-POOL",
                    transport_mode=option.mode if option else "SEA",
                    allocated_units=allocated,
                    unmet_units=unmet,
                    lead_time_days=option.lead_time_days if option else lane.lead_time_days if lane else 0,
                    priority_score=score,
                    approval_status=approval_status,
                    capacity_note=capacity_note,
                    optimizer=solver,
                    optimizer_status=solver_status,
                    decision_note=note,
                )
            )

    return sorted(decisions, key=lambda item: item.priority_score, reverse=True)


def _decisions_from_optimized_shipments(network, recommendations, priority_scores, optimized) -> list[AllocationDecision]:
    recommendation_by_key = {(item.sku, item.warehouse_id): item for item in recommendations}
    shipped_by_key: dict[tuple[str, str], list] = {}
    for shipment in optimized.shipments:
        shipped_by_key.setdefault((shipment.sku, shipment.warehouse_id), []).append(shipment)

    decisions: list[AllocationDecision] = []
    for key, shipments in shipped_by_key.items():
        item = recommendation_by_key[key]
        allocated = sum(shipment.units for shipment in shipments)
        unmet = max(0, item.recommended_units - allocated)
        mode_units: dict[str, int] = {}
        weighted_days = 0
        for shipment in shipments:
            mode_units[shipment.transport_mode] = mode_units.get(shipment.transport_mode, 0) + shipment.units
            weighted_days += shipment.lead_time_days * shipment.units
        modes = "+".join(sorted(mode_units, key=mode_units.get, reverse=True))
        lead_time_days = round(weighted_days / max(1, allocated))
        if unmet > 0:
            note = "MILP 在库存池、运输、仓容与资金约束下部分满足，缺口进入下一轮采购与调拨"
        else:
            note = "MILP 在库存池、运输、仓容与资金约束下满足本轮补货建议"
        approval_status = "待审批" if item.approval_required else "自动通过"
        decisions.append(
            AllocationDecision(
                sku=item.sku,
                warehouse_id=item.warehouse_id,
                source="GLOBAL-POOL",
                transport_mode=modes,
                allocated_units=allocated,
                unmet_units=unmet,
                lead_time_days=lead_time_days,
                priority_score=priority_scores[key],
                approval_status=approval_status,
                capacity_note=_capacity_note(network, item.warehouse_id, allocated),
                optimizer=optimized.solver,
                optimizer_status=optimized.status,
                decision_note=note,
            )
        )

    for item in recommendations:
        if item.recommended_units <= 0 or (item.sku, item.warehouse_id) in shipped_by_key:
            continue
        decisions.append(
            AllocationDecision(
                sku=item.sku,
                warehouse_id=item.warehouse_id,
                source="GLOBAL-POOL",
                transport_mode="未分配",
                allocated_units=0,
                unmet_units=item.recommended_units,
                lead_time_days=0,
                priority_score=priority_scores.get((item.sku, item.warehouse_id), 0.0),
                approval_status="待审批" if item.approval_required else "自动通过",
                capacity_note=_capacity_note(network, item.warehouse_id, 0),
                optimizer=optimized.solver,
                optimizer_status=optimized.status,
                decision_note="MILP 未在当前库存池、运输、仓容与资金约束下分配",
            )
        )

    return sorted(decisions, key=lambda item: item.priority_score, reverse=True)


def _available_warehouse_capacity(network: Network, warehouse_id: str, allocated_by_warehouse: dict[str, int]) -> int:
    constraint = network.warehouse_constraints.get(warehouse_id)
    if constraint is None:
        return 10**9
    current_position = sum(
        item.available_position for item in network.inventory.values() if item.warehouse_id == warehouse_id
    )
    already_allocated = allocated_by_warehouse.get(warehouse_id, 0)
    return max(0, min(constraint.max_daily_inbound_units, constraint.capacity_units - current_position - already_allocated))


def _capacity_note(network: Network, warehouse_id: str, planned_inbound: int) -> str:
    constraint = network.warehouse_constraints.get(warehouse_id)
    if constraint is None:
        return "未配置仓容约束"
    usage_after_plan = sum(
        item.available_position for item in network.inventory.values() if item.warehouse_id == warehouse_id
    ) + planned_inbound
    usage_rate = usage_after_plan / max(1, constraint.capacity_units)
    if usage_rate >= 0.9:
        return "接近仓容上限，需控制入仓节奏"
    if planned_inbound >= constraint.max_daily_inbound_units:
        return "已触达单日入仓处理上限"
    return "仓容与处理能力可承接"
