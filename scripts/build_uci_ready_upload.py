from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
import json
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
ZIP_PATH = ROOT / "data/public_sources/uci_online_retail.zip"
XLSX_PATH = ROOT / "data/public_sources/Online Retail.xlsx"
OUT_PATH = ROOT / "data/ready_uploads/uci_online_retail_globalpool_upload.json"
SUMMARY_PATH = ROOT / "data/ready_uploads/SOURCES.md"

WAREHOUSE_COUNTRIES = {
    "UK-BHX": {
        "country": "UK",
        "source_country": "United Kingdom",
        "region": "Europe",
        "lat": 52.45,
        "lng": -1.75,
        "capacity": 18000,
        "inbound": 5200,
        "last_mile": 0.9,
    },
    "DE-HAM": {
        "country": "DE",
        "source_country": "Germany",
        "region": "Europe",
        "lat": 53.55,
        "lng": 9.99,
        "capacity": 4200,
        "inbound": 1200,
        "last_mile": 0.88,
    },
    "FR-CDG": {
        "country": "FR",
        "source_country": "France",
        "region": "Europe",
        "lat": 49.0,
        "lng": 2.55,
        "capacity": 4200,
        "inbound": 1200,
        "last_mile": 0.87,
    },
    "AU-SYD": {
        "country": "AU",
        "source_country": "Australia",
        "region": "Oceania",
        "lat": -33.86,
        "lng": 151.21,
        "capacity": 2200,
        "inbound": 620,
        "last_mile": 0.82,
    },
}


def main() -> None:
    if not XLSX_PATH.exists():
        with ZipFile(ZIP_PATH) as archive:
            archive.extract("Online Retail.xlsx", XLSX_PATH.parent)

    workbook = load_workbook(XLSX_PATH, read_only=True, data_only=True)
    sheet = workbook.active
    headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    idx = {name: pos for pos, name in enumerate(headers)}

    rows = []
    descriptions: dict[str, Counter[str]] = defaultdict(Counter)
    unit_prices: dict[str, list[float]] = defaultdict(list)
    country_sku_qty: dict[str, Counter[str]] = defaultdict(Counter)
    sku_country_day: dict[tuple[str, str], Counter[date]] = defaultdict(Counter)
    max_day: date | None = None

    source_countries = {item["source_country"] for item in WAREHOUSE_COUNTRIES.values()}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        stock = str(row[idx["StockCode"]]).strip()
        description = str(row[idx["Description"]] or "").strip()
        quantity = row[idx["Quantity"]]
        unit_price = row[idx["UnitPrice"]]
        invoice_date = row[idx["InvoiceDate"]]
        country = row[idx["Country"]]
        if country not in source_countries or not stock or quantity is None or unit_price is None:
            continue
        if quantity <= 0 or unit_price <= 0 or not description:
            continue
        day = invoice_date.date() if hasattr(invoice_date, "date") else invoice_date
        if not isinstance(day, date):
            continue
        rows.append((stock, description, int(quantity), float(unit_price), day, country))
        descriptions[stock][description] += int(quantity)
        unit_prices[stock].append(float(unit_price))
        country_sku_qty[country][stock] += int(quantity)
        sku_country_day[(stock, country)][day] += int(quantity)
        max_day = max(max_day, day) if max_day else day

    selected_skus = select_skus(country_sku_qty)
    assert max_day is not None
    days = [max_day - timedelta(days=offset) for offset in range(29, -1, -1)]

    skus = []
    demand_history = []
    inventory = []
    market_signals = []
    global_supply_pool = []
    procurement_constraints = []

    for sku_index, stock in enumerate(selected_skus):
        avg_price = sum(unit_prices[stock]) / len(unit_prices[stock])
        name = descriptions[stock].most_common(1)[0][0]
        pack_size = [10, 24, 12][sku_index % 3]
        service_class = "premium" if sku_index < 2 else "standard"
        unit_margin = round(max(0.6, avg_price * (0.42 if service_class == "premium" else 0.35)), 2)
        sku_id = f"UCI-{stock}"
        skus.append(
            {
                "sku": sku_id,
                "name": name[:80],
                "category": "public-online-retail",
                "pack_size": pack_size,
                "unit_margin": unit_margin,
                "service_class": service_class,
            }
        )

        total_recent_demand = 0
        for warehouse_id, warehouse in WAREHOUSE_COUNTRIES.items():
            country = warehouse["source_country"]
            raw_units = [sku_country_day[(stock, country)][day] for day in days]
            daily_units = smooth_sparse_series(raw_units)
            total_recent_demand += sum(daily_units)
            demand_history.append({"sku": sku_id, "warehouse_id": warehouse_id, "daily_units": daily_units})
            recent_avg = sum(daily_units[-14:]) / 14
            on_hand = max(pack_size * 2, round_to_pack(int(recent_avg * 18), pack_size))
            in_transit = round_to_pack(int(recent_avg * 8), pack_size)
            allocated = round_to_pack(int(recent_avg * 2), pack_size)
            inventory.append(
                {
                    "sku": sku_id,
                    "warehouse_id": warehouse_id,
                    "on_hand": on_hand,
                    "in_transit": in_transit,
                    "allocated": allocated,
                }
            )
            trend = (sum(daily_units[-7:]) + 1) / max(1, sum(daily_units[-14:-7]))
            market_signals.append(
                {
                    "sku": sku_id,
                    "warehouse_id": warehouse_id,
                    "platform": "UCI Online Retail",
                    "ad_spend_index": round(1.0 + min(0.35, max(0, trend - 1) * 0.25), 3),
                    "promotion_lift": 1.18 if warehouse_id in {"UK-BHX", "DE-HAM"} else 1.1,
                    "seasonal_index": 1.22,
                    "platform_event_lift": 1.15,
                    "price_index": 1.0,
                }
            )

        pool_available = round_to_pack(max(total_recent_demand * 2, 300), pack_size)
        global_supply_pool.append(
            {"sku": sku_id, "available_units": pool_available, "reserved_units": round_to_pack(pool_available // 10, pack_size)}
        )
        procurement_constraints.append(
            {
                "sku": sku_id,
                "supplier": "PUBLIC-UCI-SIM",
                "available_units": round_to_pack(pool_available * 2, pack_size),
                "min_order_qty": pack_size * 10,
                "unit_purchase_cost": round(max(0.4, avg_price * 0.58), 2),
                "capital_budget": round(pool_available * max(0.4, avg_price * 0.58) * 0.85, 2),
            }
        )

    payload = {
        "source_metadata": {
            "primary_public_source": "UCI Machine Learning Repository - Online Retail dataset",
            "source_url": "https://archive.ics.uci.edu/dataset/352/online+retail",
            "download_url": "https://archive.ics.uci.edu/static/public/352/online+retail.zip",
            "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
            "note": "Demand history is aggregated from public transactional sales. Inventory, transport, procurement, and capital constraints are generated as simulation fields for upload testing.",
        },
        "service_level_z": {"standard": 1.28, "premium": 1.65},
        "skus": skus,
        "warehouses": build_warehouses(),
        "lanes": build_lanes(),
        "transport_options": build_transport_options(),
        "warehouse_constraints": build_warehouse_constraints(),
        "procurement_constraints": procurement_constraints,
        "global_supply_pool": global_supply_pool,
        "inventory": inventory,
        "market_signals": market_signals,
        "demand_history": demand_history,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_PATH.write_text(build_summary(selected_skus, rows), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print(f"Wrote {SUMMARY_PATH}")


def select_skus(country_sku_qty: dict[str, Counter[str]]) -> list[str]:
    source_countries = [item["source_country"] for item in WAREHOUSE_COUNTRIES.values()]
    coverage = Counter()
    total = Counter()
    for country in source_countries:
        for stock, qty in country_sku_qty[country].items():
            if qty > 0:
                coverage[stock] += 1
                total[stock] += qty
    ranked = sorted(
        (stock for stock in total if coverage[stock] >= 4),
        key=lambda stock: (coverage[stock], total[stock]),
        reverse=True,
    )
    return ranked[:3]


def smooth_sparse_series(values: list[int]) -> list[int]:
    non_zero = [value for value in values if value > 0]
    fallback = max(1, round(sum(non_zero) / len(non_zero) * 0.18)) if non_zero else 1
    smoothed = []
    for index, value in enumerate(values):
        if value > 0:
            smoothed.append(min(value, max(fallback * 9, value)))
        else:
            week_factor = 1.25 if index % 7 in (4, 5) else 0.9 if index % 7 == 0 else 1.0
            smoothed.append(max(1, round(fallback * week_factor)))
    return smoothed


def round_to_pack(value: int, pack_size: int) -> int:
    if value <= 0:
        return pack_size
    return ((value + pack_size - 1) // pack_size) * pack_size


def build_warehouses() -> list[dict]:
    return [
        {
            "warehouse_id": warehouse_id,
            "region": item["region"],
            "country": item["country"],
            "handling_capacity_per_day": item["inbound"],
            "last_mile_score": item["last_mile"],
            "latitude": item["lat"],
            "longitude": item["lng"],
        }
        for warehouse_id, item in WAREHOUSE_COUNTRIES.items()
    ]


def build_lanes() -> list[dict]:
    base = {"UK-BHX": (24, 4.3), "DE-HAM": (24, 4.1), "FR-CDG": (23, 4.0), "AU-SYD": (28, 5.2)}
    return [
        {"from": "GLOBAL-POOL", "to": warehouse_id, "lead_time_days": days, "unit_cost": cost}
        for warehouse_id, (days, cost) in base.items()
    ]


def build_transport_options() -> list[dict]:
    options = []
    sea = {"UK-BHX": (24, 4.3, 0.82, 1200), "DE-HAM": (24, 4.1, 0.84, 900), "FR-CDG": (23, 4.0, 0.84, 900), "AU-SYD": (28, 5.2, 0.8, 500)}
    air = {"UK-BHX": (7, 9.9, 0.94, 260), "DE-HAM": (6, 9.4, 0.95, 240), "FR-CDG": (6, 9.2, 0.95, 240), "AU-SYD": (8, 11.5, 0.93, 140)}
    for warehouse_id, (days, cost, reliability, max_units) in sea.items():
        options.append({"from": "GLOBAL-POOL", "to": warehouse_id, "mode": "SEA", "lead_time_days": days, "unit_cost": cost, "reliability": reliability, "max_units": max_units})
    for warehouse_id, (days, cost, reliability, max_units) in air.items():
        options.append({"from": "GLOBAL-POOL", "to": warehouse_id, "mode": "AIR", "lead_time_days": days, "unit_cost": cost, "reliability": reliability, "max_units": max_units})
    return options


def build_warehouse_constraints() -> list[dict]:
    return [
        {
            "warehouse_id": warehouse_id,
            "capacity_units": item["capacity"],
            "storage_cost_per_unit_day": 0.07 if item["region"] == "Europe" else 0.1,
            "max_daily_inbound_units": item["inbound"],
        }
        for warehouse_id, item in WAREHOUSE_COUNTRIES.items()
    ]


def build_summary(selected_skus: list[str], rows: list[tuple]) -> str:
    return f"""# Ready-to-upload public dataset

## Files

- `data/ready_uploads/uci_online_retail_globalpool_upload.json`
- Source workbook: `data/public_sources/Online Retail.xlsx`

## Public source

UCI Machine Learning Repository, Online Retail dataset:
https://archive.ics.uci.edu/dataset/352/online+retail

The UCI page describes the file as a transactional dataset for a UK-based non-store online retailer, covering 2010-12-01 to 2011-12-09. It includes `StockCode`, `Description`, `Quantity`, `InvoiceDate`, `UnitPrice`, and `Country`.

## Transformation

- Selected SKU stock codes: {", ".join(selected_skus)}
- Countries mapped to warehouses: United Kingdom -> UK-BHX, Germany -> DE-HAM, France -> FR-CDG, Australia -> AU-SYD.
- `demand_history` is aggregated from public transactions.
- Sparse daily demand is smoothed only to make it usable as operational daily forecast input.
- Inventory, transport, procurement, capital, and market signal fields are simulation fields generated from demand scale.

## Row count used

Filtered positive-quantity rows across selected countries: {len(rows)}
"""


if __name__ == "__main__":
    main()
