"""Prespecified synthetic market ablation. Simulated timestamps are NOT ingestion backfill.

Regimes and seeds are fixed. No hyperparameters are changed based on outer scores.
The repository sample has no certified signal history and must show exact fallback.
"""

from __future__ import annotations
import argparse, hashlib, json, random, sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from overseas_inventory.models import DemandSeries, load_network
from aegis.planning.backtest import rolling_backtest
from aegis.planning.forecast import MARKET_MODEL_VERSION
from aegis.planning.market_forecast import (
    FEATURES,
    RIDGE,
    TRAIN_WINDOW,
    MIN_MARKET_TRAIN,
    MIN_COVERAGE,
)
from aegis.core.util import content_hash


def corpus(length=210):
    histories = []
    skus = {}
    revisions = []
    start = date(2026, 1, 1)
    for regime in ("informative", "noise", "late_receipt", "late_revision"):
        for seed in (101, 103, 107, 109):
            rng = random.Random(f"{regime}:{seed}")
            code = f"{regime}:{seed}"
            values = []
            events = [rng.random() < 0.35 for _ in range(length + 35)]
            for i, promo in enumerate(events):
                day = start + timedelta(days=i)
                pub = datetime.combine(day - timedelta(days=45), time(12), UTC)
                event = 1.8 if promo else 1.0
                if i < length:
                    lift = 15 * (event - 1) if regime != "noise" else 0
                    values.append(
                        max(
                            0,
                            round(
                                20 + lift + 2 * (i % 7 in (5, 6)) + rng.gauss(0, 1.5)
                            ),
                        )
                    )
                received = (
                    pub + timedelta(hours=1)
                    if regime != "late_receipt"
                    else datetime.combine(day + timedelta(days=2), time(12), UTC)
                )
                row = {
                    "id": f"{code}:{i}:1",
                    "sku": code,
                    "warehouse_id": "W",
                    "source_system": "mock:market",
                    "platform": "amazon",
                    "signal_kind": "planned",
                    "signal_date": day.isoformat(),
                    "revision": 1,
                    "published_at": pub.isoformat(),
                    "received_at": received.isoformat(),
                    "available_at": received.isoformat(),
                    "is_deleted": False,
                    **{k: 1.0 for k in FEATURES},
                    "promotion_lift": event,
                }
                revisions.append(row)
                if regime == "late_revision":
                    correction = datetime.combine(
                        day + timedelta(days=5), time(12), UTC
                    )
                    revisions.append(
                        row
                        | {
                            "id": f"{code}:{i}:2",
                            "revision": 2,
                            "published_at": correction.isoformat(),
                            "received_at": correction.isoformat(),
                            "available_at": correction.isoformat(),
                            "ad_spend_index": 2.0,
                        }
                    )
            histories.append(
                DemandSeries(
                    code, "W", values, calendar_start=start.isoformat(), timezone="UTC"
                )
            )
            skus[code] = SimpleNamespace(category=regime)
    return SimpleNamespace(
        demand_history=histories, skus=skus, market_signal_revisions=revisions
    )


def compact(report):
    models = report["models"]
    return {
        k: v for k, v in report.items() if k not in ("folds", "models", "metrics")
    } | {
        "models": {
            name: {
                "overall": m["overall"],
                "vs_mean28": m["vs_baseline"],
                "vs_univariate": m["vs_univariate"],
                "by_regime": {
                    k: v for k, v in m["by_group"].items() if k.startswith("category:")
                },
            }
            for name, m in models.items()
        }
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-folds", type=int, default=8)
    p.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/artifacts/market-signal-comparison.json",
    )
    args = p.parse_args()
    fixture = corpus()
    sample = load_network(ROOT / "data/sample_network.json")
    report = {
        "model_version": MARKET_MODEL_VERSION,
        "scope": "synthetic simulated as-of fixture plus repository no-signal control; no merchant evidence",
        "frozen_parameters": {
            "ridge": RIDGE,
            "train_window": TRAIN_WINDOW,
            "minimum_training_rows": MIN_MARKET_TRAIN,
            "minimum_coverage": MIN_COVERAGE,
        },
        "fixture_digest": content_hash(
            {
                "history": [vars(s) for s in fixture.demand_history],
                "revisions": fixture.market_signal_revisions,
            }
        ),
        "source_sha256": {},
        "runs": {},
    }
    for name, network in [("synthetic", fixture), ("repository_no_signals", sample)]:
        report["runs"][name] = {}
        for horizon in (7, 14, 30):
            result = rolling_backtest(
                network,
                horizon=horizon,
                max_folds_per_series=args.max_folds or None,
                model_version=MARKET_MODEL_VERSION,
                include_folds=False,
            )
            report["runs"][name][str(horizon)] = compact(result)
            print(
                name,
                horizon,
                {
                    m: result["models"][m]["overall"]["wape"]
                    for m in ("adaptive", "market_ridge", "market_adaptive")
                },
                result["market_comparison"]["market_adaptive"],
                flush=True,
            )
    for f in (
        "src/aegis/planning/market_forecast.py",
        "src/aegis/planning/forecast.py",
        "src/aegis/planning/backtest.py",
        "scripts/benchmark_market_signals.py",
    ):
        report["source_sha256"][f] = hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
