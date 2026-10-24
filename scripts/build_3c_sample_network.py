from __future__ import annotations

import json
import math
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "sample_network.json"
DAYS = 180


WAREHOUSES = [
    {
        "warehouse_id": "US-LAX",
        "region": "North America",
        "country": "US",
        "handling_capacity_per_day": 6500,
        "last_mile_score": 0.92,
        "latitude": 34.05,
        "longitude": -118.24,
        "capacity_units": 26000,
        "storage_cost_per_unit_day": 0.028,
        "max_daily_inbound_units": 5200,
        "market_multiplier": 1.35,
        "lead_time": 25,
        "sea_cost": 1.15,
        "air_cost": 3.8,
    },
    {
        "warehouse_id": "DE-FRA",
        "region": "Europe",
        "country": "DE",
        "handling_capacity_per_day": 4300,
        "last_mile_score": 0.88,
        "latitude": 50.11,
        "longitude": 8.68,
        "capacity_units": 18000,
        "storage_cost_per_unit_day": 0.032,
        "max_daily_inbound_units": 3400,
        "market_multiplier": 0.95,
        "lead_time": 32,
        "sea_cost": 1.42,
        "air_cost": 4.25,
    },
    {
        "warehouse_id": "UK-BHX",
        "region": "Europe",
        "country": "UK",
        "handling_capacity_per_day": 3600,
        "last_mile_score": 0.86,
        "latitude": 52.49,
        "longitude": -1.89,
        "capacity_units": 14000,
        "storage_cost_per_unit_day": 0.034,
        "max_daily_inbound_units": 2800,
        "market_multiplier": 0.78,
        "lead_time": 30,
        "sea_cost": 1.35,
        "air_cost": 4.05,
    },
    {
        "warehouse_id": "AU-SYD",
        "region": "Oceania",
        "country": "AU",
        "handling_capacity_per_day": 2600,
        "last_mile_score": 0.82,
        "latitude": -33.87,
        "longitude": 151.21,
        "capacity_units": 9600,
        "storage_cost_per_unit_day": 0.04,
        "max_daily_inbound_units": 1900,
        "market_multiplier": 0.58,
        "lead_time": 28,
        "sea_cost": 1.62,
        "air_cost": 4.75,
    },
]


SKUS = [
    {"sku": "CAB-C1-BLK", "name": "USB-C 快充线 1 米黑色", "category": "USB-C 快充线", "pack_size": 50, "unit_margin": 2.4, "service_class": "standard", "base": 38, "purchase": 2.2, "volume": 1.0},
    {"sku": "CAB-C2-BLK", "name": "USB-C 快充线 2 米黑色", "category": "USB-C 快充线", "pack_size": 50, "unit_margin": 2.9, "service_class": "standard", "base": 31, "purchase": 2.6, "volume": 1.1},
    {"sku": "CAB-C1-WHT", "name": "USB-C 快充线 1 米白色", "category": "USB-C 快充线", "pack_size": 50, "unit_margin": 2.3, "service_class": "standard", "base": 26, "purchase": 2.1, "volume": 1.0},
    {"sku": "STAND-DESK", "name": "手机桌面支架", "category": "手机支架", "pack_size": 30, "unit_margin": 4.2, "service_class": "standard", "base": 18, "purchase": 4.4, "volume": 1.8},
    {"sku": "STAND-CAR", "name": "手机车载支架", "category": "手机支架", "pack_size": 30, "unit_margin": 4.8, "service_class": "standard", "base": 15, "purchase": 5.1, "volume": 2.0},
    {"sku": "STAND-MAG", "name": "磁吸手机支架", "category": "手机支架", "pack_size": 30, "unit_margin": 5.6, "service_class": "premium", "base": 12, "purchase": 6.0, "volume": 2.1},
    {"sku": "APCASE-PRO-CLR", "name": "AirPods Pro 透明保护壳", "category": "蓝牙耳机保护壳", "pack_size": 40, "unit_margin": 3.1, "service_class": "standard", "base": 20, "purchase": 3.2, "volume": 0.9},
    {"sku": "APCASE-PRO-SIL", "name": "AirPods Pro 硅胶保护壳", "category": "蓝牙耳机保护壳", "pack_size": 40, "unit_margin": 3.4, "service_class": "standard", "base": 17, "purchase": 3.5, "volume": 1.0},
    {"sku": "APCASE-GEN3", "name": "AirPods 3 代保护壳", "category": "蓝牙耳机保护壳", "pack_size": 40, "unit_margin": 2.9, "service_class": "standard", "base": 14, "purchase": 3.0, "volume": 0.9},
    {"sku": "IPAD10-CASE", "name": "iPad 10 代保护壳", "category": "平板保护壳", "pack_size": 20, "unit_margin": 8.8, "service_class": "premium", "base": 8, "purchase": 9.8, "volume": 3.8},
    {"sku": "IPADAIR-CASE", "name": "iPad Air 保护壳", "category": "平板保护壳", "pack_size": 20, "unit_margin": 9.4, "service_class": "premium", "base": 7, "purchase": 10.5, "volume": 3.6},
    {"sku": "HUB-5IN1", "name": "USB-C 拓展坞 5 合 1", "category": "USB-C 拓展坞", "pack_size": 12, "unit_margin": 14.5, "service_class": "premium", "base": 6, "purchase": 15.8, "volume": 2.2},
    {"sku": "HUB-7IN1", "name": "USB-C 拓展坞 7 合 1", "category": "USB-C 拓展坞", "pack_size": 12, "unit_margin": 19.0, "service_class": "premium", "base": 5, "purchase": 20.5, "volume": 2.4},
    {"sku": "LAPSTAND-FOLD", "name": "折叠笔记本支架", "category": "笔记本支架", "pack_size": 16, "unit_margin": 10.5, "service_class": "standard", "base": 6, "purchase": 12.0, "volume": 4.2},
    {"sku": "LAPSTAND-LIFT", "name": "升降笔记本支架", "category": "笔记本支架", "pack_size": 16, "unit_margin": 13.2, "service_class": "premium", "base": 4, "purchase": 14.8, "volume": 5.1},
]


def main() -> None:
    rng = random.Random(20260609)
    demand_history = []
    inventory = []
    market_signals = []
    global_supply_pool = []
    procurement_constraints = []

    for sku_index, sku in enumerate(SKUS):
        total_recent = 0
        for warehouse in WAREHOUSES:
            units = build_series(sku, warehouse, sku_index, rng)
            demand_history.append(
                {"sku": sku["sku"], "warehouse_id": warehouse["warehouse_id"], "daily_units": units}
            )
            recent_avg = sum(units[-30:]) / 30
            total_recent += sum(units[-30:])
            coverage_days = inventory_coverage_days(sku["sku"], warehouse["warehouse_id"])
            in_transit_days = 4 if warehouse["warehouse_id"] in {"US-LAX", "AU-SYD"} else 7
            inventory.append(
                {
                    "sku": sku["sku"],
                    "warehouse_id": warehouse["warehouse_id"],
                    "on_hand": round_to_pack(int(recent_avg * coverage_days), sku["pack_size"]),
                    "in_transit": round_to_pack(int(recent_avg * in_transit_days), sku["pack_size"]),
                    "allocated": round_to_pack(int(recent_avg * 2), sku["pack_size"]),
                }
            )
            market_signals.append(build_signal(sku, warehouse))

        pool_available = round_to_pack(int(total_recent * 1.55 + sku["pack_size"] * 8), sku["pack_size"])
        global_supply_pool.append(
            {
                "sku": sku["sku"],
                "available_units": pool_available,
                "reserved_units": round_to_pack(int(pool_available * 0.12), sku["pack_size"]),
            }
        )
        procurement_constraints.append(
            {
                "sku": sku["sku"],
                "supplier": "深圳中心仓-3C配件供应链",
                "available_units": round_to_pack(int(pool_available * 1.8), sku["pack_size"]),
                "min_order_qty": sku["pack_size"] * 4,
                "unit_purchase_cost": sku["purchase"],
                "capital_budget": round(pool_available * sku["purchase"] * (0.78 if sku["service_class"] == "premium" else 0.68), 2),
            }
        )

    payload = {
        "source_metadata": {
            "project_title": "面向 3C 配件跨境电商的海外仓智能补货与分仓系统设计",
            "scenario": "中国跨境电商卖家从深圳中心仓向美国、德国、英国、澳洲海外仓补货。",
            "category_focus": "3C 配件",
            "simulation_days": DAYS,
            "sku_count": len(SKUS),
            "warehouse_count": len(WAREHOUSES),
            "display_scenarios": [
                "美国仓 USB-C 拓展坞爆单缺货",
                "德国仓耳机保护壳积压而英国仓不足",
                "澳洲海运延迟，高毛利 SKU 改走空运",
            ],
        },
        "service_level_z": {"standard": 1.28, "premium": 1.65},
        "skus": [
            {
                "sku": item["sku"],
                "name": item["name"],
                "category": item["category"],
                "pack_size": item["pack_size"],
                "unit_margin": item["unit_margin"],
                "service_class": item["service_class"],
            }
            for item in SKUS
        ],
        "warehouses": [
            {
                "warehouse_id": item["warehouse_id"],
                "region": item["region"],
                "country": item["country"],
                "handling_capacity_per_day": item["handling_capacity_per_day"],
                "last_mile_score": item["last_mile_score"],
                "latitude": item["latitude"],
                "longitude": item["longitude"],
            }
            for item in WAREHOUSES
        ],
        "lanes": [
            {
                "from": "GLOBAL-POOL",
                "to": item["warehouse_id"],
                "lead_time_days": item["lead_time"],
                "unit_cost": item["sea_cost"],
            }
            for item in WAREHOUSES
        ],
        "transport_options": build_transport_options(),
        "warehouse_constraints": [
            {
                "warehouse_id": item["warehouse_id"],
                "capacity_units": item["capacity_units"],
                "storage_cost_per_unit_day": item["storage_cost_per_unit_day"],
                "max_daily_inbound_units": item["max_daily_inbound_units"],
            }
            for item in WAREHOUSES
        ],
        "procurement_constraints": procurement_constraints,
        "global_supply_pool": global_supply_pool,
        "inventory": inventory,
        "market_signals": market_signals,
        "demand_history": demand_history,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print(f"SKUs: {len(SKUS)}, warehouses: {len(WAREHOUSES)}, demand series: {len(demand_history)}")


def build_series(sku: dict, warehouse: dict, sku_index: int, rng: random.Random) -> list[int]:
    values = []
    for day in range(DAYS):
        weekday = day % 7
        weekend = 1.18 if weekday in {5, 6} else 1.0
        trend = 0.9 + 0.22 * (day / (DAYS - 1))
        seasonal = 1 + 0.08 * math.sin((day + sku_index * 5) / 14)
        event = event_multiplier(day, sku["sku"], warehouse["warehouse_id"])
        noise = rng.uniform(0.88, 1.14)
        units = sku["base"] * warehouse["market_multiplier"] * weekend * trend * seasonal * event * noise
        values.append(max(1, int(round(units))))
    return values


def event_multiplier(day: int, sku: str, warehouse_id: str) -> float:
    multiplier = 1.0
    if 46 <= day <= 58 and sku in {"LAPSTAND-FOLD", "LAPSTAND-LIFT", "IPAD10-CASE", "IPADAIR-CASE"}:
        multiplier *= 1.28
    if 122 <= day <= 138 and sku.startswith("HUB") and warehouse_id == "US-LAX":
        multiplier *= 1.72
    if 136 <= day <= 150 and sku.startswith("CAB"):
        multiplier *= 1.24
    if 150 <= day <= 172 and sku.startswith("APCASE") and warehouse_id == "UK-BHX":
        multiplier *= 1.3
    if 150 <= day <= 172 and sku.startswith("APCASE") and warehouse_id == "DE-FRA":
        multiplier *= 0.86
    if 158 <= day <= 179 and warehouse_id == "AU-SYD" and sku in {"HUB-5IN1", "HUB-7IN1", "LAPSTAND-LIFT"}:
        multiplier *= 1.35
    return multiplier


def inventory_coverage_days(sku: str, warehouse_id: str) -> int:
    if warehouse_id == "US-LAX" and sku in {"HUB-5IN1", "HUB-7IN1"}:
        return 9
    if warehouse_id == "UK-BHX" and sku.startswith("APCASE"):
        return 11
    if warehouse_id == "DE-FRA" and sku.startswith("APCASE"):
        return 58
    if warehouse_id == "AU-SYD" and sku in {"HUB-5IN1", "HUB-7IN1", "LAPSTAND-LIFT"}:
        return 10
    if sku.startswith("CAB"):
        return 21
    if sku.startswith("HUB"):
        return 18
    if "LAPSTAND" in sku:
        return 24
    return 26


def build_signal(sku: dict, warehouse: dict) -> dict:
    signal = {
        "sku": sku["sku"],
        "warehouse_id": warehouse["warehouse_id"],
        "platform": "Amazon / TikTok Shop / eBay",
        "ad_spend_index": 1.12,
        "promotion_lift": 1.08,
        "seasonal_index": 1.05,
        "platform_event_lift": 1.06,
        "price_index": 1.0,
    }
    if warehouse["warehouse_id"] == "US-LAX" and sku["sku"].startswith("HUB"):
        signal.update({"ad_spend_index": 1.48, "promotion_lift": 1.32, "seasonal_index": 1.16, "platform_event_lift": 1.42})
    elif warehouse["warehouse_id"] == "UK-BHX" and sku["sku"].startswith("APCASE"):
        signal.update({"ad_spend_index": 1.28, "promotion_lift": 1.22, "platform_event_lift": 1.18})
    elif warehouse["warehouse_id"] == "AU-SYD" and sku["service_class"] == "premium":
        signal.update({"ad_spend_index": 1.2, "promotion_lift": 1.12, "seasonal_index": 1.18, "platform_event_lift": 1.12})
    elif sku["sku"].startswith("CAB"):
        signal.update({"ad_spend_index": 1.18, "promotion_lift": 1.16, "platform_event_lift": 1.1})
    return signal


def build_transport_options() -> list[dict]:
    options = []
    for item in WAREHOUSES:
        sea_lead = item["lead_time"] + (10 if item["warehouse_id"] == "AU-SYD" else 0)
        options.extend(
            [
                {
                    "from": "GLOBAL-POOL",
                    "to": item["warehouse_id"],
                    "mode": "SEA",
                    "lead_time_days": sea_lead,
                    "unit_cost": item["sea_cost"],
                    "reliability": 0.83 if item["warehouse_id"] == "AU-SYD" else 0.9,
                    "max_units": int(item["max_daily_inbound_units"] * 1.8),
                },
                {
                    "from": "GLOBAL-POOL",
                    "to": item["warehouse_id"],
                    "mode": "AIR",
                    "lead_time_days": 7 if item["warehouse_id"] != "AU-SYD" else 9,
                    "unit_cost": item["air_cost"],
                    "reliability": 0.97,
                    "max_units": int(item["max_daily_inbound_units"] * 0.55),
                },
            ]
        )
    return options


def round_to_pack(quantity: int, pack_size: int) -> int:
    if quantity <= 0:
        return pack_size
    return max(pack_size, round(quantity / pack_size) * pack_size)


if __name__ == "__main__":
    main()
