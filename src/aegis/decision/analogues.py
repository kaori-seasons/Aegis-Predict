"""Then-known, age-aligned reference cards. Sales under censoring stay lower bounds."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ..core.errors import InvalidInput
from ..core.util import content_hash
from ..planning.commerce_features import known, launch_events, stamp


def month_end(start, months):
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def sales_window(series, launch, end, cutoff):
    begin = date.fromisoformat(series["calendar_start"])
    values = series["daily_units"]
    missing = series.get("missing_days") or [False] * len(values)
    censored = series.get("stockout_days") or [False] * len(values)
    if len(missing) != len(values) or len(censored) != len(values):
        raise InvalidInput("invalid frozen demand masks")
    elapsed = max(0, min((end - launch).days, (cutoff - launch).days))
    observed = 0
    gaps = 0
    stockout = 0
    for offset in range(elapsed):
        i = (launch + timedelta(days=offset) - begin).days
        if i < 0 or i >= len(values) or missing[i]:
            gaps += 1
            continue
        observed += values[i]
        stockout += bool(censored[i])
    expected = (end - launch).days
    status = (
        "immature"
        if elapsed < expected
        else "incomplete_coverage"
        if gaps
        else "censored_lower_bound"
        if stockout
        else "complete"
    )
    return {
        "start": str(launch),
        "end_exclusive": str(end),
        "expected_days": expected,
        "elapsed_days": elapsed,
        "observed_days": elapsed - gaps,
        "missing_days": gaps,
        "stockout_days": stockout,
        "observed_sales": observed,
        "status": status,
        "demand_interpretation": "complete_observed_sales"
        if status == "complete"
        else "observed_sales_lower_bound_not_latent_demand",
    }


def evidence(payload, sku, warehouse, launch_date):
    net = payload["network"]
    ctx = net.get("commerce_context", {})
    origin = stamp(payload["as_of"])
    config = ctx.get("config", {})
    series = {
        r["sku"]: r for r in net["demand_history"] if r["warehouse_id"] == warehouse
    }
    if sku not in series:
        raise InvalidInput("target absent from frozen demand scope")
    local_day = origin.astimezone(ZoneInfo(series[sku].get("timezone", "UTC"))).date()
    events = launch_events(ctx, origin)
    target = next(
        (
            r
            for r in events
            if r["sku"] == sku
            and r["event_kind"] == "planned_launch"
            and str(r["event_date"]) == str(launch_date)
        ),
        None,
    )
    if not target or launch_date <= local_day:
        raise InvalidInput(
            "register a then-known, strictly future planned launch before starting expert review"
        )
    profiles = known(
        [
            r
            for r in ctx.get("launch_profiles", [])
            if r["source_system"] == config.get("launch_profile_source")
            and r["warehouse_id"] == warehouse
            and r["channel"] == config.get("launch_signal_channel", "*")
        ],
        origin,
        ("sku", "warehouse_id", "channel"),
    )
    profiles = {r["sku"]: r for r in profiles}

    def profile(code):
        p = profiles.get(code)
        return {
            k: p.get(k) if p else None
            for k in (
                "positioning",
                "pricing_strategy",
                "retail_price",
                "currency",
                "price_basis",
                "launch_plan",
                "initial_supply_units",
                "first_batch_scope",
                "id",
                "revision",
                "source_system",
                "published_at",
                "received_at",
                "available_at",
            )
        }

    location = payload.get("ids", {}).get("locations", {}).get(warehouse)
    contracts = [
        r
        for r in payload.get("watermarks", {}).get("authorities", [])
        if r["domain"] == "demand" and r["location_id"] == location
    ]
    channel = config.get("launch_signal_channel", "*")
    channel_matches = bool(contracts) and {r["channel"] for r in contracts} == {channel}
    refs = [
        r
        for r in events
        if r["event_kind"] == "first_sale"
        and r["sku"] in series
        and r["sku"] != sku
        and r["cohort_key"] == target["cohort_key"]
        and r["launch_group"] != target["launch_group"]
        and date.fromisoformat(str(r["event_date"])) <= local_day
    ]
    refs = sorted(refs, key=lambda r: (str(r["event_date"]), r["sku"]), reverse=True)
    cards = []
    for r in refs[:12]:
        s = series[r["sku"]]
        launch = date.fromisoformat(str(r["event_date"]))
        completed = origin.astimezone(ZoneInfo(s.get("timezone", "UTC"))).date()
        p = profile(r["sku"])
        windows = {
            "D30": sales_window(s, launch, launch + timedelta(days=30), completed),
            "M3": sales_window(s, launch, month_end(launch, 3), completed),
            "M6": sales_window(s, launch, month_end(launch, 6), completed),
        }
        denom = p["initial_supply_units"]
        ratio = (
            windows["D30"]["observed_sales"] / denom
            if denom
            and p["first_batch_scope"] == "warehouse_channel"
            and channel_matches
            and windows["D30"]["status"] == "complete"
            else None
        )
        cards.append(
            {
                "sku": r["sku"],
                "launch_group": r["launch_group"],
                "launch_date": str(launch),
                "window_anchor": "first_confirmed_sale",
                "warehouse_id": warehouse,
                "profile": p,
                "windows": windows,
                "d30_sales_to_initial_allocation": ratio,
                "ratio_note": "ratio of observed base-unit sales to first allocation, not a calibrated probability; restocking can exceed1"
                if ratio is not None
                else "unavailable: incomplete/censored sales, unknown allocation, or incompatible quantity/channel scope",
            }
        )
    upcoming = [
        {
            "sku": r["sku"],
            "date": str(r["event_date"]),
            "launch_group": r["launch_group"],
        }
        for r in events
        if r["event_kind"] == "planned_launch"
        and r["sku"] != sku
        and r["cohort_key"] == target["cohort_key"]
        and date.fromisoformat(str(r["event_date"])) >= local_day
    ]
    return {
        "schema": "launch-analogue-evidence-v1",
        "as_of": origin.isoformat(),
        "target": {
            "sku": sku,
            "warehouse_id": warehouse,
            "channel": channel,
            "launch_date": str(launch_date),
            "cohort_key": target["cohort_key"],
            "launch_group": target["launch_group"],
            "profile": {
                k: v
                for k, v in profile(sku).items()
                if k not in ("initial_supply_units", "first_batch_scope")
            },
        },
        "analogs": cards,
        "upcoming_products": upcoming,
        "support": {
            "reference_rows": len(cards),
            "distinct_launch_groups": len({r["launch_group"] for r in cards}),
            "eligible_rows_before_limit": len(refs),
        },
        "quantity_contract": {
            "unit": "frozen canonical base units",
            "channel_compatible": channel_matches,
            "demand_contracts": contracts,
        },
        "selection": "same tenant, warehouse and then-known cohort; exclude target launch entity; latest launch dates first, never rank by realized sales",
        "limitations": [
            "Reference age starts at the first confirmed sale, not listing exposure; verify launch-to-first-sale delay before comparing. No listing date is inferred.",
            "No uncensoring or imputation in these reference cards. Immature windows are not full-window outcomes.",
            "D30 is30calendar days; M3/M6 are calendar-month anniversaries, not90/180days.",
            "Initial production for all markets cannot be used as the denominator of one warehouse/channel.",
            "Reference rows and expert agreement do not prove causal price effects, demand accuracy or calibrated intervals.",
            "Profile values are the latest then-known revision, not a reconstructed launch-date price; verify applicability before comparison.",
        ],
        "input_digest": content_hash(payload),
    }
