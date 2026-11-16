from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt

from .forecasting import ForecastResult, forecast_all
from .models import Network


@dataclass(frozen=True)
class ReplenishmentRecommendation:
    sku: str
    warehouse_id: str
    model_name: str
    daily_rate: float
    p80_units: int
    p95_units: int
    lead_time_days: int
    inventory_position: int
    reorder_point: int
    target_stock: int
    recommended_units: int
    urgency: float
    approval_required: bool
    reason: str


def round_up_to_pack(quantity: int, pack_size: int) -> int:
    if quantity <= 0:
        return 0
    return ceil(quantity / pack_size) * pack_size


def build_replenishment_recommendations(
    network: Network,
    horizon_days: int = 30,
    review_period_days: int = 7,
) -> list[ReplenishmentRecommendation]:
    if horizon_days <= 0 or review_period_days <= 0:
        raise ValueError("horizon and review period must be positive")
    forecasts = forecast_all(
        network.demand_history,
        horizon_days=horizon_days,
        signals=network.market_signals,
    )
    recommendations: list[ReplenishmentRecommendation] = []

    for forecast in forecasts:
        sku = network.skus[forecast.sku]
        lane = network.lane_from_pool(forecast.warehouse_id)
        inventory = network.inventory[(forecast.sku, forecast.warehouse_id)]
        if lane is None:
            continue

        safety_z = network.service_level_z.get(sku.service_class, 1.28)
        safety_stock = safety_z * sqrt(
            lane.lead_time_days * forecast.volatility ** 2
            + forecast.daily_rate ** 2 * lane.lead_time_std_days ** 2
        )
        lead_time_demand = forecast.daily_rate * lane.lead_time_days
        reorder_point = ceil(lead_time_demand + safety_stock)
        protection = lane.lead_time_days + review_period_days
        target_safety = safety_z * sqrt(
            protection * forecast.volatility ** 2
            + forecast.daily_rate ** 2 * lane.lead_time_std_days ** 2
        )
        target_stock = ceil(forecast.daily_rate * protection + target_safety)
        raw_need = max(0, target_stock - inventory.available_position)
        recommended_units = round_up_to_pack(raw_need, sku.pack_size)

        stockout_pressure = max(0.0, (reorder_point - inventory.available_position) / max(1, reorder_point))
        forecast_risk = 1 - forecast.confidence
        urgency = round(min(1.0, stockout_pressure * 0.82 + forecast_risk * 0.18), 3)

        if recommended_units == 0:
            reason = "库存位置覆盖预测周期，暂不触发补货"
        elif urgency >= 0.65:
            reason = "库存低于补货点，需优先从库存池分配"
        else:
            reason = "库存低于目标水位，建议按预测需求滚动补货"
        approval_required = recommended_units * sku.unit_margin >= 1200 or urgency >= 0.55

        recommendations.append(
            ReplenishmentRecommendation(
                sku=forecast.sku,
                warehouse_id=forecast.warehouse_id,
                model_name=forecast.model_name,
                daily_rate=forecast.daily_rate,
                p80_units=forecast.p80_units,
                p95_units=forecast.p95_units,
                lead_time_days=lane.lead_time_days,
                inventory_position=inventory.available_position,
                reorder_point=reorder_point,
                target_stock=target_stock,
                recommended_units=recommended_units,
                urgency=urgency,
                approval_required=approval_required,
                reason=reason,
            )
        )

    return sorted(recommendations, key=lambda item: (item.urgency, item.recommended_units), reverse=True)
