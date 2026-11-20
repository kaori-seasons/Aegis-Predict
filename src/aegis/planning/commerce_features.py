"""Point-in-time commerce/public features. Missing, censored and zero stay distinct."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo

from ..core.util import content_hash

VERSION = "commerce-lag-contract-v1"
METRICS = ("sessions", "orders", "units", "ad_spend", "revenue", "unit_price")
LAGS = (1, 7, 28)
PUBLIC = ("fx_rate", "pageviews")


def stamp(value):
    value = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if value.tzinfo is None:
        raise ValueError("naive knowledge time is forbidden")
    return value.astimezone(UTC)


def known(rows, origin, identity):
    """Filter all knowledge clocks BEFORE revision ranking, including tombstones."""
    origin = stamp(origin)
    latest = {}
    for r in rows:
        if any(
            stamp(r[k]) > origin
            for k in ("published_at", "received_at", "available_at")
        ):
            continue
        key = tuple(r[k] for k in identity)
        prior = latest.get(key)
        if prior is None or int(r["revision"]) > int(prior["revision"]):
            latest[key] = r
    return [r for r in latest.values() if not r.get("is_deleted")]


def launch_events(context, origin):
    config = context.get("config", {})
    return known(
        [
            r
            for r in context.get("sku_launches", [])
            if r["source_system"] == config.get("launch_signal_source")
            and r["channel"] == config.get("launch_signal_channel", "*")
        ],
        origin,
        ("sku", "channel", "event_kind"),
    )


def features(context, sku, warehouse, origin, timezone):
    origin = stamp(origin)
    day = origin.astimezone(ZoneInfo(timezone)).date()
    config = context.get("config", {})
    rows = known(
        [
            r
            for r in context.get("commerce_performance", [])
            if r["sku"] == sku
            and r["warehouse_id"] == warehouse
            and r["source_system"] == config.get("commerce_signal_source")
            and r["channel"] == config.get("commerce_signal_channel")
            and r["contract_version"]
            == config.get("commerce_contract_version", "commerce-daily-v1")
        ],
        origin,
        ("sku", "warehouse_id", "channel", "metric", "metric_date", "contract_version"),
    )
    daily = {}
    ids = []
    for r in rows:
        if r["timezone"] != timezone or stamp(r["period_end_at"]) > origin:
            continue
        d = date.fromisoformat(str(r["metric_date"]))
        if not day - timedelta(days=28) <= d < day:
            continue
        daily[(r["metric"], d)] = r
        ids.append(r["id"])
    values = {}
    currencies = {}
    for metric in METRICS:
        for lag in LAGS:
            eligible = [
                daily[(metric, day - timedelta(days=i))]
                for i in range(1, lag + 1)
                if (metric, day - timedelta(days=i)) in daily
            ]
            observed = [
                r
                for r in eligible
                if r["state"] == "observed" and r["value"] is not None
            ]
            currency = {r["currency"] for r in observed}
            # Never average incompatible currencies. An absent day is not an observed zero.
            if len(currency) > 1:
                observed = []
            name = f"{metric}_lag{lag}"
            values[name] = fmean(r["value"] for r in observed) if observed else None
            values[name + "_coverage"] = len(observed) / lag
            currencies[name] = sorted(currency)
    for lag in LAGS:
        pairs = [
            (
                daily.get(("sessions", day - timedelta(days=i))),
                daily.get(("orders", day - timedelta(days=i))),
            )
            for i in range(1, lag + 1)
        ]
        pairs = [
            (s, o)
            for s, o in pairs
            if s
            and o
            and s["state"] == o["state"] == "observed"
            and s["value"] is not None
            and o["value"] is not None
        ]
        denominator = sum(s["value"] for s, o in pairs)
        values[f"orders_per_session_lag{lag}"] = (
            sum(o["value"] for s, o in pairs) / denominator if denominator > 0 else None
        )
        values[f"orders_per_session_lag{lag}_coverage"] = len(pairs) / lag
    bindings = known(
        [
            r
            for r in context.get("public_signal_bindings", [])
            if r["sku"] == sku
            and r["warehouse_id"] == warehouse
            and r["source_system"] == config.get("public_signal_source")
        ],
        origin,
        ("sku", "warehouse_id", "provider", "series_key", "metric"),
    )
    # More than one series for one semantic metric is ambiguous; refuse to blend them.
    counts = {
        m: sum(
            r["metric"] == m and date.fromisoformat(str(r["effective_from"])) <= day
            for r in bindings
        )
        for m in (*PUBLIC, "public_holiday")
    }
    bindings = [
        r
        for r in bindings
        if date.fromisoformat(str(r["effective_from"])) <= day
        and counts[r["metric"]] == 1
    ]
    signals = known(
        [
            r
            for r in context.get("public_signals", [])
            if r["source_system"] == config.get("public_signal_source")
        ],
        origin,
        ("provider", "series_key", "metric", "signal_date"),
    )
    public = {}
    holidays = {}
    for binding in bindings:
        ids.append(binding["id"])
        for r in signals:
            if any(r[k] != binding[k] for k in ("provider", "series_key", "metric")):
                continue
            d = date.fromisoformat(str(r["signal_date"]))
            if d < date.fromisoformat(str(binding["effective_from"])):
                continue
            if r["signal_kind"] == "planned" and r["metric"] == "public_holiday":
                holidays[str(d)] = r["value"]
                ids.append(r["id"])
            elif (
                stamp(r["period_end_at"]) <= origin
                and day - timedelta(days=28) <= d < day
            ):
                public[(r["metric"], d)] = r["value"]
                ids.append(r["id"])
    for metric in PUBLIC:
        for lag in LAGS:
            seen = [
                public[(metric, day - timedelta(days=i))]
                for i in range(1, lag + 1)
                if public.get((metric, day - timedelta(days=i))) is not None
            ]
            values[f"{metric}_lag{lag}"] = fmean(seen) if seen else None
            values[f"{metric}_lag{lag}_coverage"] = len(seen) / lag
    values["public_holiday_lag28_coverage"] = (
        sum(str(day - timedelta(days=i)) in holidays for i in range(1, 29)) / 28
    )
    return {
        "version": VERSION,
        "origin": origin.isoformat(),
        "local_day": str(day),
        "values": values,
        "currencies": currencies,
        "known_holidays": holidays,
        "revision_ids": sorted(set(ids)),
        "digest": content_hash([VERSION, sorted(set(ids)), values]),
        "ambiguous_public_metrics": [m for m, c in counts.items() if c > 1],
    }


def local_issue(day, timezone):
    return datetime.combine(day, time.min, ZoneInfo(timezone)).astimezone(UTC)
