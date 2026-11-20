"""Strict independent metric contracts and append-only server-timed publication."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from math import isfinite
from re import fullmatch
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from ..core.util import content_hash
from ..models import (
    CommercePerformanceRevision,
    Location,
    PublicSignalBindingRevision,
    PublicSignalRevision,
    SkuLaunchRevision,
)

REVISION_STREAMS = (
    "commerce_performance",
    "sku_launches",
    "public_signals",
    "public_signal_bindings",
)
METRICS = {
    "sessions": "count",
    "orders": "count",
    "units": "count",
    "ad_spend": "money",
    "revenue": "money",
    "unit_price": "money",
}
PUBLIC_METRICS = {
    "frankfurter": ("fx_rate", "quote_per_base", "observed"),
    "wikimedia": ("pageviews", "count", "observed"),
    "govuk_holidays": ("public_holiday", "indicator", "planned"),
}
TABLES = {
    "commerce_performance": CommercePerformanceRevision,
    "sku_launches": SkuLaunchRevision,
    "public_signals": PublicSignalRevision,
    "public_signal_bindings": PublicSignalBindingRevision,
}
IDENTITIES = {
    "commerce_performance": (
        "sku_id",
        "location_id",
        "channel",
        "metric",
        "metric_date",
        "contract_version",
    ),
    "sku_launches": ("sku_id", "channel", "event_kind"),
    "public_signals": ("provider", "series_key", "metric", "signal_date"),
    "public_signal_bindings": (
        "sku_id",
        "location_id",
        "provider",
        "series_key",
        "metric",
    ),
}
CONSTRAINTS = {
    "commerce_performance": "uq_commerce_revision_identity",
    "sku_launches": "uq_launch_revision_identity",
    "public_signals": "uq_public_revision_identity",
    "public_signal_bindings": "uq_public_binding_identity",
}


def _limited(value, label, length):
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise ValueError(f"invalid {label}")
    return value.strip()


def build_values(stream, r, ctx):
    v = {k: r[k] for k in ("revision", "published_at", "is_deleted")}
    if stream in ("commerce_performance", "sku_launches", "public_signal_bindings"):
        v["sku_id"] = ctx.sku_id(r["sku"])
    if stream in ("commerce_performance", "public_signal_bindings"):
        v["location_id"] = ctx.location_id(r["location"])
    if stream == "commerce_performance":
        metric = r["metric"]
        state = r["state"]
        currency = r["currency"] or ""
        if metric not in METRICS or r["unit"] != METRICS[metric]:
            raise ValueError("metric/unit contract mismatch")
        if state not in ("observed", "missing", "censored"):
            raise ValueError("invalid metric coverage state")
        value = r["value"]
        if value is not None and (not isfinite(value) or not 0 <= value < 1e15):
            raise ValueError("invalid metric value")
        if state == "observed" and value is None:
            raise ValueError("observed metric requires a value; missing is not zero")
        if METRICS[metric] == "money" and (not fullmatch(r"[A-Z]{3}", currency)):
            raise ValueError("money metrics require one uppercase ISO currency")
        if METRICS[metric] == "count" and (
            currency or value is not None and value != int(value)
        ):
            raise ValueError("count metrics require whole counts without currency")
        tz = ZoneInfo(r["timezone"])
        # MappingContext's calendar contract is resolved below by the ingestion session.
        v.update(
            channel=_limited(r["channel"], "channel", 80),
            contract_version=_limited(r["contract_version"], "metric contract", 60),
            metric=metric,
            metric_date=r["date"],
            period_end_at=datetime.combine(
                r["date"] + timedelta(days=1), time.min, tz
            ).astimezone(UTC),
            timezone=r["timezone"],
            unit=r["unit"],
            currency=currency,
            state=state,
            value=value,
        )
    elif stream == "sku_launches":
        if r["event_kind"] not in ("planned_launch", "first_sale"):
            raise ValueError("invalid launch event kind")
        v.update(
            channel=_limited(r["channel"], "channel", 80),
            launch_group=_limited(r["launch_group"], "launch entity group", 120),
            cohort_key=_limited(r["cohort_key"], "launch cohort", 120),
            event_kind=r["event_kind"],
            event_date=r["date"],
            note=_limited(r["note"], "launch evidence", 2000),
        )
    elif stream in ("public_signals", "public_signal_bindings"):
        provider = r["provider"]
        if provider not in PUBLIC_METRICS or r["metric"] != PUBLIC_METRICS[provider][0]:
            raise ValueError("unknown public provider or metric")
        from .connectors.public_market import validate

        series = validate(
            {"subscriptions": [{"provider": provider, "series_key": r["series_key"]}]}
        )["subscriptions"][0]["series_key"]
        v.update(
            provider=provider,
            series_key=_limited(series, "public series", 240),
            metric=r["metric"],
        )
        if stream == "public_signal_bindings":
            v.update(
                effective_from=r["effective_from"],
                note=_limited(r["note"], "mapping reason", 2000),
            )
        else:
            metric, unit, kind = PUBLIC_METRICS[provider]
            if r["unit"] != unit or r["signal_kind"] != kind:
                raise ValueError("public metric contract mismatch")
            value = r["value"]
            if value is not None and (not isfinite(value) or not 0 <= value < 1e15):
                raise ValueError("invalid public signal value")
            if metric == "public_holiday" and value not in (0, 1):
                raise ValueError(
                    "holiday is a calendar indicator, not a demand multiplier"
                )
            if metric == "fx_rate" and (value is None or value <= 0):
                raise ValueError("exchange reference rate must be positive")
            if r["publication_evidence"] not in ("provider_timestamp", "first_seen"):
                raise ValueError("publication evidence required")
            v.update(
                signal_date=r["date"],
                period_end_at=datetime.combine(
                    r["date"] + timedelta(days=1), time.min, UTC
                ),
                signal_kind=kind,
                unit=unit,
                value=value,
                publication_evidence=r["publication_evidence"],
                attribution=_limited(r["attribution"], "provider attribution", 500),
            )
    if stream != "public_signals" and (
        type(v["revision"]) is not int or v["revision"] < 1
    ):
        raise ValueError("explicit source revisions must be positive")
    return v


def append_revision(session, tenant_id, source, stream, raw, values, now):
    values = dict(values)
    if stream == "commerce_performance":
        loc = session.get(Location, values["location_id"])
        if loc is None or loc.kind != "warehouse" or loc.timezone != values["timezone"]:
            raise ValueError(
                "commerce day timezone must match its explicitly assigned location"
            )
        if values["period_end_at"] > now:
            raise ValueError(
                "observed commerce metrics must describe a completed local day"
            )
    if (
        stream == "public_signals"
        and values["signal_kind"] == "observed"
        and values["period_end_at"] > now
    ):
        raise ValueError("observed public data requires a completed UTC day")
    if (
        stream == "sku_launches"
        and values["event_kind"] == "first_sale"
        and values["event_date"] > now.date()
    ):
        raise ValueError("a future planned launch is not an observed first sale")
    if stream == "public_signals" and values["publication_evidence"] == "first_seen":
        values["published_at"] = raw.received_at
    elif values.get("published_at") is None:
        raise ValueError("source publication timestamp is required")
    if values["published_at"] > now + timedelta(minutes=5):
        raise ValueError("future publication timestamps are not currently available")
    table = TABLES[stream]
    identity = IDENTITIES[stream]
    query = select(table).where(
        table.tenant_id == tenant_id,
        table.source_system == source,
        *(getattr(table, k) == values[k] for k in identity),
    )
    if stream == "public_signals" and values["revision"] == 0:
        # Provider endpoints expose latest values, not a provider revision number.
        # Serialize per series/day; identical values never obtain a new known_at.
        key = int(
            content_hash(
                [str(tenant_id), source, stream, *(str(values[k]) for k in identity)]
            )[:15],
            16,
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        previous = session.scalar(query.order_by(table.revision.desc()).limit(1))
        stable = {
            k: v for k, v in values.items() if k not in ("published_at", "revision")
        }
        if previous:
            old = {k: getattr(previous, k) for k in stable}
            if content_hash(old) == content_hash(stable):
                return False
        values["revision"] = previous.revision + 1 if previous else 1
    digest = content_hash(values)
    data = {
        **values,
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "source_system": source,
        "raw_record_id": raw.id,
        "received_at": raw.received_at,
        "available_at": max(now, raw.received_at, values["published_at"]),
        "content_hash": digest,
    }
    result = session.scalar(
        insert(table)
        .values(data)
        .on_conflict_do_nothing(constraint=CONSTRAINTS[stream])
        .returning(table.id)
    )
    if result:
        return True
    previous = session.scalar(query.where(table.revision == values["revision"]))
    if previous is None or previous.content_hash != digest:
        raise ValueError(
            "immutable revision has conflicting content; publish a new revision"
        )
    return False
