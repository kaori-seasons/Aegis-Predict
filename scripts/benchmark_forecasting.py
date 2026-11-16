"""Repeatable model comparison on repository samples and fixed synthetic seeds.

This is a retrospective validation fixture, never a claim about merchant accuracy.
The candidate grid and inner selector are not tuned by this script.
"""

from __future__ import annotations
import argparse, hashlib, json, random, sys
from pathlib import Path
from types import SimpleNamespace
from statistics import fmean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from overseas_inventory.models import DemandSeries, load_network
from aegis.planning.backtest import rolling_backtest
from aegis.planning.forecast import (
    MODEL_VERSION,
    CANDIDATES,
    INNER_MIN_FOLDS,
    INNER_MAX_FOLDS,
    MIN_RELATIVE_GAIN,
)


def validation_network(length=252):
    series = []
    skus = {}
    for regime in (
        "steady",
        "weekly",
        "trend_up",
        "trend_down",
        "level_shift",
        "intermittent",
        "obsolete",
        "missing",
        "censored",
    ):
        for seed in (101, 103, 107, 109, 113, 127):
            rng = random.Random(f"{regime}:{seed}")
            values = []
            missing = []
            stockout = []
            for t in range(length):
                if regime == "weekly":
                    mean = (6, 9, 11, 14, 14, 18, 21)[t % 7]
                elif regime == "trend_up":
                    mean = 3 + 0.12 * t
                elif regime == "trend_down":
                    mean = max(0, 45 - 0.15 * t)
                elif regime == "level_shift":
                    mean = 8 if t < length // 2 else 25
                else:
                    mean = 12
                if regime in ("intermittent", "obsolete"):
                    value = (
                        rng.randint(8, 24)
                        if rng.random() < 0.2
                        and not (regime == "obsolete" and t >= length // 2)
                        else 0
                    )
                else:
                    value = max(0, round(rng.gauss(mean, 2)))
                m = regime == "missing" and rng.random() < 0.04
                c = regime == "censored" and rng.random() < 0.03
                values.append(0 if m else min(value, 2) if c else value)
                missing.append(m)
                stockout.append(c)
            code = f"{regime}:{seed}"
            series.append(DemandSeries(code, "W", values, stockout, missing))
            skus[code] = SimpleNamespace(category=regime)
    return SimpleNamespace(demand_history=series, skus=skus)


def series_summary(model):
    pairs = [
        p for key, p in model["paired_by_group"].items() if key.startswith("series:")
    ]
    skills = [p["skill"] for p in pairs if p["skill"] is not None]
    return {
        "series": len(pairs),
        "lower_error": sum(p["absolute_error_difference"] < -1e-5 for p in pairs),
        "higher_error": sum(p["absolute_error_difference"] > 1e-5 for p in pairs),
        "equal_error": sum(abs(p["absolute_error_difference"]) <= 1e-5 for p in pairs),
        "macro_skill": fmean(skills) if skills else None,
        "macro_skill_eligible_series": len(skills),
        "zero_baseline_series": sum(p["baseline_error"] <= 1e-9 for p in pairs),
    }


def compact(report):
    return {
        k: v for k, v in report.items() if k not in ("folds", "metrics", "models")
    } | {
        "models": {
            m: {
                "overall": v["overall"],
                "vs_mean28": v["vs_baseline"],
                "series_summary": series_summary(v),
                "by_regime": {
                    g: {"metrics": x, "vs_mean28": v["paired_by_group"][g]}
                    for g, x in v["by_group"].items()
                    if g.startswith("category:")
                },
            }
            for m, v in report["models"].items()
        }
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--output", type=Path, default=ROOT / "docs/artifacts/forecast-comparison.json"
    )
    p.add_argument(
        "--max-folds",
        type=int,
        default=8,
        help="recent N origins per series; 0 means all",
    )
    args = p.parse_args()
    corpus = validation_network()
    sample = load_network(ROOT / "data/sample_network.json")

    def digest(network):
        return hashlib.sha256(
            json.dumps(
                [vars(s) for s in network.demand_history], sort_keys=True
            ).encode()
        ).hexdigest()

    result = {
        "model_version": MODEL_VERSION,
        "candidate_grid": CANDIDATES,
        "selector": {
            "minimum_folds": INNER_MIN_FOLDS,
            "maximum_folds": INNER_MAX_FOLDS,
            "minimum_gain": MIN_RELATIVE_GAIN,
        },
        "scope": "retrospective frozen-series comparison; synthetic and repository fixtures, not merchant UAT",
        "corpus_digest": {
            "repository": digest(sample),
            "synthetic_validation": digest(corpus),
        },
        "runs": {},
    }
    for name, network in [("repository", sample), ("synthetic_validation", corpus)]:
        result["runs"][name] = {}
        for horizon in (7, 14, 30):
            report = rolling_backtest(
                network,
                horizon=horizon,
                max_folds_per_series=args.max_folds or None,
                calendar_aligned=name == "synthetic_validation",
                include_folds=False,
            )
            result["runs"][name][str(horizon)] = compact(report)
            print(
                name,
                horizon,
                {
                    m: round(report["models"][m]["overall"]["wape"], 6)
                    if report["models"][m]["overall"]["wape"] is not None
                    else None
                    for m in ("adaptive", "mean_28", "legacy_v2")
                },
                "skill",
                report["models"]["adaptive"]["vs_baseline"]["skill"],
            )
    paths = [
        "src/aegis/planning/forecast.py",
        "src/aegis/planning/forecast_legacy.py",
        "src/aegis/planning/backtest.py",
        "scripts/benchmark_forecasting.py",
    ]
    result["source_sha256"] = {
        f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in paths
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
