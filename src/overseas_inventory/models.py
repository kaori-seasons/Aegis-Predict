from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SKU:
    sku: str
    name: str
    category: str
    pack_size: int
    unit_margin: float
    service_class: str


@dataclass(frozen=True)
class Warehouse:
    warehouse_id: str
    region: str
    country: str
    handling_capacity_per_day: int
    last_mile_score: float
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Lane:
    source: str
    target: str
    lead_time_days: int
    unit_cost: float
    lead_time_std_days: float = 0.0


@dataclass(frozen=True)
class InventoryPosition:
    sku: str
    warehouse_id: str
    on_hand: int
    in_transit: int
    allocated: int
    on_order: int = 0
    backorder: int = 0

    @property
    def available_position(self) -> int:
        return self.on_hand + self.in_transit + self.on_order - self.allocated - self.backorder


@dataclass(frozen=True)
class DemandSeries:
    sku: str
    warehouse_id: str
    daily_units: list[int]
    stockout_days: list[bool] | None = None
    missing_days: list[bool] | None = None
    calendar_start: str | None = None
    timezone: str = 'UTC'


@dataclass(frozen=True)
class MarketSignal:
    sku: str
    warehouse_id: str
    platform: str
    ad_spend_index: float
    promotion_lift: float
    seasonal_index: float
    platform_event_lift: float
    price_index: float


@dataclass(frozen=True)
class TransportOption:
    source: str
    target: str
    mode: str
    lead_time_days: int
    unit_cost: float
    reliability: float
    max_units: int


@dataclass(frozen=True)
class WarehouseConstraint:
    warehouse_id: str
    capacity_units: int
    storage_cost_per_unit_day: float
    max_daily_inbound_units: int


@dataclass(frozen=True)
class ProcurementConstraint:
    sku: str
    supplier: str
    available_units: int
    min_order_qty: int
    unit_purchase_cost: float
    capital_budget: float


@dataclass(frozen=True)
class PoolSupply:
    sku: str
    available_units: int
    reserved_units: int

    @property
    def allocatable_units(self) -> int:
        return max(0, self.available_units - self.reserved_units)


@dataclass(frozen=True)
class Network:
    service_level_z: dict[str, float]
    skus: dict[str, SKU]
    warehouses: dict[str, Warehouse]
    lanes: list[Lane]
    inventory: dict[tuple[str, str], InventoryPosition]
    demand_history: list[DemandSeries]
    global_supply_pool: dict[str, PoolSupply]
    market_signals: dict[tuple[str, str], MarketSignal]
    transport_options: list[TransportOption]
    warehouse_constraints: dict[str, WarehouseConstraint]
    procurement_constraints: dict[str, ProcurementConstraint]
    market_signal_revisions: list[dict] = field(default_factory=list)
    forecast_as_of: str | None = None

    def lane_from_pool(self, warehouse_id: str) -> Lane | None:
        for lane in self.lanes:
            if lane.source == "GLOBAL-POOL" and lane.target == warehouse_id:
                return lane
        return None

    def signal_for(self, sku: str, warehouse_id: str) -> MarketSignal | None:
        return self.market_signals.get((sku, warehouse_id))

    def transport_option(self, warehouse_id: str, urgency: float) -> TransportOption | None:
        candidates = [
            option
            for option in self.transport_options
            if option.source == "GLOBAL-POOL" and option.target == warehouse_id
        ]
        if not candidates:
            return None
        if urgency >= 0.6:
            return sorted(candidates, key=lambda item: (-item.reliability, item.lead_time_days))[0]
        return sorted(candidates, key=lambda item: (item.unit_cost, -item.reliability))[0]


def load_network(path: str | Path) -> Network:
    return network_from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


def network_from_payload(payload: dict[str, Any]) -> Network:
    """Build a ``Network`` from the ``sample_network.json`` document shape (also produced by the DB assembler)."""

    skus = {
        row["sku"]: SKU(
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            pack_size=int(row["pack_size"]),
            unit_margin=float(row["unit_margin"]),
            service_class=row["service_class"],
        )
        for row in payload["skus"]
    }
    warehouses = {
        row["warehouse_id"]: Warehouse(
            warehouse_id=row["warehouse_id"],
            region=row["region"],
            country=row["country"],
            handling_capacity_per_day=int(row["handling_capacity_per_day"]),
            last_mile_score=float(row["last_mile_score"]),
            latitude=float(row.get("latitude", 0.0)),
            longitude=float(row.get("longitude", 0.0)),
        )
        for row in payload["warehouses"]
    }
    lanes = [
        Lane(
            source=row["from"],
            target=row["to"],
            lead_time_days=int(row["lead_time_days"]),
            unit_cost=float(row["unit_cost"]),
            lead_time_std_days=float(row.get("lead_time_std_days", 0.0)),
        )
        for row in payload["lanes"]
    ]
    inventory = {
        (row["sku"], row["warehouse_id"]): InventoryPosition(
            sku=row["sku"],
            warehouse_id=row["warehouse_id"],
            on_hand=int(row["on_hand"]),
            in_transit=int(row["in_transit"]),
            allocated=int(row["allocated"]),
            on_order=int(row.get("on_order", 0)),
            backorder=int(row.get("backorder", 0)),
        )
        for row in payload["inventory"]
    }
    demand_history = [
        DemandSeries(
            sku=row["sku"],
            warehouse_id=row["warehouse_id"],
            daily_units=[int(value) for value in row["daily_units"]],
            stockout_days=row.get("stockout_days"),
            missing_days=row.get("missing_days"),
            calendar_start=row.get('calendar_start'),
            timezone=row.get('timezone', 'UTC'),
        )
        for row in payload["demand_history"]
    ]
    global_supply_pool = {
        row["sku"]: PoolSupply(
            sku=row["sku"],
            available_units=int(row["available_units"]),
            reserved_units=int(row["reserved_units"]),
        )
        for row in payload["global_supply_pool"]
    }
    market_signals = {
        (row["sku"], row["warehouse_id"]): MarketSignal(
            sku=row["sku"],
            warehouse_id=row["warehouse_id"],
            platform=row["platform"],
            ad_spend_index=float(row["ad_spend_index"]),
            promotion_lift=float(row["promotion_lift"]),
            seasonal_index=float(row["seasonal_index"]),
            platform_event_lift=float(row["platform_event_lift"]),
            price_index=float(row["price_index"]),
        )
        for row in payload.get("market_signals", [])
    }
    transport_options = [
        TransportOption(
            source=row["from"],
            target=row["to"],
            mode=row["mode"],
            lead_time_days=int(row["lead_time_days"]),
            unit_cost=float(row["unit_cost"]),
            reliability=float(row["reliability"]),
            max_units=int(row["max_units"]),
        )
        for row in payload.get("transport_options", [])
    ]
    warehouse_constraints = {
        row["warehouse_id"]: WarehouseConstraint(
            warehouse_id=row["warehouse_id"],
            capacity_units=int(row["capacity_units"]),
            storage_cost_per_unit_day=float(row["storage_cost_per_unit_day"]),
            max_daily_inbound_units=int(row["max_daily_inbound_units"]),
        )
        for row in payload.get("warehouse_constraints", [])
    }
    procurement_constraints = {
        row["sku"]: ProcurementConstraint(
            sku=row["sku"],
            supplier=row["supplier"],
            available_units=int(row["available_units"]),
            min_order_qty=int(row["min_order_qty"]),
            unit_purchase_cost=float(row["unit_purchase_cost"]),
            capital_budget=float(row["capital_budget"]),
        )
        for row in payload.get("procurement_constraints", [])
    }

    return Network(
        service_level_z={key: float(value) for key, value in payload["service_level_z"].items()},
        skus=skus,
        warehouses=warehouses,
        lanes=lanes,
        inventory=inventory,
        demand_history=demand_history,
        global_supply_pool=global_supply_pool,
        market_signals=market_signals,
        transport_options=transport_options,
        warehouse_constraints=warehouse_constraints,
        procurement_constraints=procurement_constraints,
        market_signal_revisions=payload.get('market_signal_revisions', []),
        forecast_as_of=payload.get('forecast_as_of'),
    )


def dataclass_to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {field: dataclass_to_dict(getattr(value, field)) for field in value.__dataclass_fields__}
    if isinstance(value, list):
        return [dataclass_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: dataclass_to_dict(item) for key, item in value.items()}
    return value
