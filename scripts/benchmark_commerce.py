"""Reproducible CONTROLLED signal experiments, never a claim about merchant uplift.

PYTHONPATH=src .venv/bin/python scripts/benchmark_commerce.py --output docs/artifacts/commerce-benchmark.json
Optional --export-dir writes standalone Network inputs for `aegis commerce-backtest`.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from aegis.decision.jobs import code_digest
from aegis.planning.commerce_backtest import backtest
from overseas_inventory.models import network_from_payload

START = date(2026, 1, 1)


def row(**values):
    return {
        "revision": 1,
        "is_deleted": False,
        "published_at": "2025-01-01T00:00:00+00:00",
        "received_at": "2025-01-01T00:00:00+00:00",
        "available_at": "2025-01-01T00:00:00+00:00",
        **values,
    }


def fixture(case, n=210):
    payload = {
        "skus": [],
        "warehouses": [
            {
                "warehouse_id": "W",
                "region": "test",
                "country": "GB",
                "handling_capacity_per_day": 1000,
                "last_mile_score": 1,
            }
        ],
        "lanes": [],
        "inventory": [],
        "demand_history": [],
        "global_supply_pool": [],
        "service_level_z": {"standard": 1.28},
        "forecast_as_of": datetime.combine(
            START + timedelta(days=n), datetime.min.time(), UTC
        ).isoformat(),
    }
    context = {
        "config": {
            "commerce_signal_source": "c",
            "commerce_signal_channel": "amazon",
            "commerce_contract_version": "commerce-daily-v1",
            "public_signal_source": "p",
            "launch_signal_source": "c",
            "launch_signal_channel": "*",
        },
        "commerce_performance": [],
        "sku_launches": [],
        "public_signals": [],
        "public_signal_bindings": [],
    }
    payload["commerce_context"] = context
    codes = [f"S{i}" for i in range(5)] if case == "cold_grouped" else ["S"]
    for index, code in enumerate(codes):
        payload["skus"].append(
            {
                "sku": code,
                "name": "Controlled synthetic product",
                "category": "fixture",
                "pack_size": 1,
                "unit_margin": 1,
                "service_class": "standard",
            }
        )
        launch = START + timedelta(days=(180 if index == 4 else index * 20))
        values = []
        for i in range(n):
            day = START + timedelta(days=i)
            clock = datetime.combine(
                day + timedelta(days=1), datetime.min.time(), UTC
            ).isoformat()
            signal = 100 + ((i * 37) % 101) * 10
            previous = 100 + (((i - 1) * 37) % 101) * 10
            value = 10 + int(previous / 20)
            if case == "public_irrelevant":
                value = 20 + (i % 7) * 3
            if case == "no_signal":
                value = 10 + (i % 14)
            if case == "cold_grouped":
                value = max(0, 10 + (day - launch).days) if day >= launch else 0
            values.append(value)
            if case == "commerce_predictive":
                context["commerce_performance"].append(
                    row(
                        id=f"c{i}",
                        source_system="c",
                        sku=code,
                        warehouse_id="W",
                        channel="amazon",
                        contract_version="commerce-daily-v1",
                        metric="sessions",
                        metric_date=str(day),
                        period_end_at=clock,
                        timezone="UTC",
                        state="observed",
                        unit="count",
                        currency="",
                        value=signal,
                        published_at=clock,
                        received_at=clock,
                        available_at=clock,
                    )
                )
            if case.startswith("public_"):
                context["public_signals"].append(
                    row(
                        id=f"p{i}",
                        source_system="p",
                        provider="wikimedia",
                        series_key="en.wikipedia.org|USB-C",
                        metric="pageviews",
                        signal_date=str(day),
                        period_end_at=clock,
                        signal_kind="observed",
                        value=signal,
                        published_at=clock,
                        received_at=clock,
                        available_at=clock,
                        publication_evidence="controlled_fixture",
                    )
                )
        payload["demand_history"].append(
            {
                "sku": code,
                "warehouse_id": "W",
                "daily_units": values,
                "calendar_start": str(START),
                "timezone": "UTC",
            }
        )
        if case == "cold_grouped":
            context["sku_launches"].append(
                row(
                    id=f"l{index}",
                    source_system="c",
                    sku=code,
                    channel="*",
                    launch_group=f"product-{index}",
                    cohort_key="fixture",
                    event_kind="first_sale",
                    event_date=str(launch),
                    published_at=datetime.combine(
                        launch, datetime.min.time(), UTC
                    ).isoformat(),
                    received_at=datetime.combine(
                        launch, datetime.min.time(), UTC
                    ).isoformat(),
                    available_at=datetime.combine(
                        launch, datetime.min.time(), UTC
                    ).isoformat(),
                )
            )
    if case.startswith("public_"):
        context["public_signal_bindings"].append(
            row(
                id="binding",
                source_system="p",
                sku="S",
                warehouse_id="W",
                provider="wikimedia",
                series_key="en.wikipedia.org|USB-C",
                metric="pageviews",
                effective_from=str(START),
            )
        )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path)
    args = parser.parse_args()
    result = {
        "schema": "controlled-commerce-experiment-v1",
        "kernel_digest": code_digest(),
        "simulated_knowledge_history": True,
        "release_eligible": False,
        "cases": {},
    }
    for case in (
        "commerce_predictive",
        "public_predictive",
        "public_irrelevant",
        "no_signal",
        "cold_grouped",
    ):
        payload = fixture(case)
        if args.export_dir:
            args.export_dir.mkdir(parents=True, exist_ok=True)
            (args.export_dir / (case + ".json")).write_text(json.dumps(payload))
        result["cases"][case] = {
            str(h): backtest(network_from_payload(payload), horizon=h, max_folds=4)
            for h in (1, 7, 28)
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {h: r["tracks"] for h, r in v.items()}
                for k, v in result["cases"].items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
