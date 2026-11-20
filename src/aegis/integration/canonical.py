"""Canonical upserts with version guard + content hash (plan 5.5).

Every stream has a ``build`` step (typed record -> table row, resolving identifiers through the
``MappingContext``) and an ``upsert`` step. The guard on ``ON CONFLICT DO UPDATE`` means:

* an older source version never overwrites a newer one (out-of-order delivery is harmless);
* an identical payload never rewrites the row (no write amplification, no spurious events).

A missing ``source_updated_at`` on either side disables the ordering check (last write wins),
because the source gave us nothing to order by.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Table, and_, func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..core.util import content_hash
from ..models import (
    DailyDemand,
    DemandObservation,
    FxRate,
    InventorySnapshot,
    Lane,
    Location,
    MarketSignal,
    MarketSignalRevision,
    OrderLine,
    PurchaseOrderLine,
    Sku,
    SkuComponent,
    SupplierTerms,
    TransportOption,
)
from .mapping import MappingContext

MAPPER_VERSION = "canonical-v2"
CANCELLED_ORDER_STATUSES = ("cancelled", "canceled", "void", "rejected")


@dataclass
class CanonicalRow:
    stream: str
    key: tuple
    values: dict[str, Any]
    raw_id: int
    source_updated_at: datetime | None


@dataclass
class UpsertResult:
    changed: int
    changed_keys: list[tuple]
    demand_days_rebuilt: int = 0


def _hash(values: dict[str, Any], exclude: tuple[str, ...] = ()) -> str:
    return content_hash({key: value for key, value in values.items() if key not in exclude})


@dataclass(frozen=True)
class StreamWriter:
    table: Any
    conflict: tuple[str, ...]
    build: Callable[[dict[str, Any], MappingContext], dict[str, Any]]
    # Columns forming the business key inside ``values`` (used for dedupe + events).
    key_columns: tuple[str, ...]
    versioned: bool = True
    hashed: bool = True
    # Extra SQL guard appended to the update predicate.
    extra_guard: Callable[[Table, Any], Any] | None = None


# -- builders: typed record -> row values (raise MappingIssue on unknown identifiers) ----------


def _build_sku(record: dict[str, Any], _ctx: MappingContext) -> dict[str, Any]:
    return {
        "code": record["sku"],
        "name": record["name"],
        "category": record["category"] or "",
        "pack_size": record["pack_size"],
        "unit_margin": record["unit_margin"],
        "service_class": record["service_class"] or "standard",
        "is_active": record["is_active"] if record["is_active"] is not None else True,
    }


def _build_location(record: dict[str, Any], _ctx: MappingContext) -> dict[str, Any]:
    return {
        "code": record["code"],
        "name": record["name"] or record["code"],
        "kind": record["kind"],
        "region": record["region"] or "",
        "country": (record["country"] or "")[:8],
        "timezone": record["timezone"] or "UTC",
        "handling_capacity_per_day": record["handling_capacity_per_day"] or 0,
        "last_mile_score": record["last_mile_score"] or 0.0,
        "latitude": record["latitude"] or 0.0,
        "longitude": record["longitude"] or 0.0,
        "capacity_units": record["capacity_units"],
        "storage_cost_per_unit_day": record["storage_cost_per_unit_day"],
        "max_daily_inbound_units": record["max_daily_inbound_units"],
        "is_active": record["is_active"] if record["is_active"] is not None else True,
    }


def _build_component(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "parent_sku_id": ctx.sku_id(record["parent_sku"]),
        "component_sku_id": ctx.sku_id(record["component_sku"]),
        "quantity": record["quantity"],
    }


def _build_lane(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "from_location_id": ctx.location_id(record["from"]),
        "to_location_id": ctx.location_id(record["to"]),
        "lead_time_days": record["lead_time_days"],
        "lead_time_std_days": record["lead_time_std_days"],
        "unit_cost": record["unit_cost"],
    }


def _build_transport(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "from_location_id": ctx.location_id(record["from"]),
        "to_location_id": ctx.location_id(record["to"]),
        "mode": record["mode"].upper(),
        "lead_time_days": record["lead_time_days"],
        "lead_time_std_days": record["lead_time_std_days"],
        "unit_cost": record["unit_cost"],
        "reliability": record["reliability"],
        "max_units": record["max_units"],
    }


def _build_supplier(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "sku_id": ctx.sku_id(record["sku"]),
        "supplier": record["supplier"],
        "available_units": record["available_units"],
        "min_order_qty": record["min_order_qty"] or 0,
        "unit_purchase_cost": record["unit_purchase_cost"],
        "capital_budget": record["capital_budget"],
        "currency": (record["currency"] or "USD").upper()[:3],
    }


def _build_snapshot(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "sku_id": ctx.sku_id(record["sku"]),
        "location_id": ctx.location_id(record["location"]),
        "as_of": record["as_of"],
        "on_hand": record["on_hand"],
        "in_transit": record["in_transit"] or 0,
        "allocated": record["allocated"] or 0,
        "backorder": record["backorder"] or 0,
    }


def _build_po(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "external_po_id": record["po_id"][:120],
        "external_line_id": record["line_id"][:120],
        "sku_id": ctx.sku_id(record["sku"]),
        "location_id": ctx.location_id(record["location"]),
        "supplier": record["supplier"] or "",
        "status": record["status"].lower(),
        "qty_ordered": record["qty_ordered"],
        "qty_received": record["qty_received"] or 0,
        "expected_arrival": record["expected_arrival"],
    }


def _build_order_line(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    from .demand_status import eligible
    return {
        "external_order_id": record["order_id"][:120],
        "external_line_id": record["line_id"][:120],
        "sku_id": ctx.sku_id(record["sku"]),
        "location_id": ctx.location_id(record["location"]),
        "channel": record["channel"] or "",
        "quantity": record["quantity"],
        "status": record["status"].lower(),
        "demand_eligible": eligible(ctx.platform, record["status"]),
        "ordered_at": record["ordered_at"],
        "unit_price": record["unit_price"],
        "currency": (record["currency"] or None) and record["currency"].upper()[:3],
    }


def _build_daily_demand(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "sku_id": ctx.sku_id(record["sku"]),
        "location_id": ctx.location_id(record["location"]),
        "demand_date": record["date"],
        "units": record["units"],
        "kind": "daily_demand",
        "channel": record.get("channel") or "*",
        "state": record.get("observation_state") or ("censored" if record["stockout"] else "observed"),
    }


def _build_signal(record: dict[str, Any], ctx: MappingContext) -> dict[str, Any]:
    return {
        "sku_id": ctx.sku_id(record["sku"]),
        "location_id": ctx.location_id(record["location"]),
        "signal_date": record["date"],
        "signal_kind": record["signal_kind"],
        "revision": record["revision"],
        "published_at": record["published_at"],
        "is_deleted": record["is_deleted"],
        "platform": record["platform"] or "",
        "ad_spend_index": record["ad_spend_index"],
        "promotion_lift": record["promotion_lift"],
        "seasonal_index": record["seasonal_index"],
        "platform_event_lift": record["platform_event_lift"],
        "price_index": record["price_index"],
    }


def _build_fx(record: dict[str, Any], _ctx: MappingContext) -> dict[str, Any]:
    return {
        "base_currency": record["base"].upper()[:3],
        "quote_currency": record["quote"].upper()[:3],
        "rate_date": record["date"],
        "rate": record["rate"],
    }


WRITERS: dict[str, StreamWriter] = {
    "skus": StreamWriter(Sku, ("tenant_id", "code"), _build_sku, ("code",)),
    "locations": StreamWriter(Location, ("tenant_id", "code"), _build_location, ("code",)),
    "sku_components": StreamWriter(
        SkuComponent,
        ("tenant_id", "parent_sku_id", "component_sku_id"),
        _build_component,
        ("parent_sku_id", "component_sku_id"),
        versioned=False,
        hashed=False,
    ),
    "lanes": StreamWriter(
        Lane, ("tenant_id", "from_location_id", "to_location_id"), _build_lane, ("from_location_id", "to_location_id")
    ),
    "transport_options": StreamWriter(
        TransportOption,
        ("tenant_id", "from_location_id", "to_location_id", "mode"),
        _build_transport,
        ("from_location_id", "to_location_id", "mode"),
    ),
    "supplier_terms": StreamWriter(SupplierTerms, ("tenant_id", "sku_id", "supplier"), _build_supplier, ("sku_id", "supplier")),
    "inventory_snapshots": StreamWriter(
        InventorySnapshot,
        ("tenant_id", "sku_id", "location_id", "as_of", "source_system"),
        _build_snapshot,
        ("sku_id", "location_id", "as_of"),
        versioned=False,
    ),
    "purchase_orders": StreamWriter(
        PurchaseOrderLine,
        ("tenant_id", "source_system", "external_po_id", "external_line_id"),
        _build_po,
        ("external_po_id", "external_line_id"),
    ),
    "order_lines": StreamWriter(
        OrderLine,
        ("tenant_id", "source_system", "external_order_id", "external_line_id"),
        _build_order_line,
        ("external_order_id", "external_line_id"),
    ),
    "daily_demand": StreamWriter(
        DemandObservation,
        ("tenant_id", "source_system", "kind", "sku_id", "location_id", "channel", "demand_date"),
        _build_daily_demand,
        ("sku_id", "location_id", "channel", "demand_date"),
        versioned=True,
        hashed=False,
    ),
    "market_signals": StreamWriter(
        MarketSignalRevision,
        ('tenant_id','source_system','sku_id','location_id','platform','signal_kind','signal_date','revision'),
        _build_signal, ('sku_id','location_id','platform','signal_kind','signal_date','revision'), versioned=False,
    ),
    "fx_rates": StreamWriter(
        FxRate, ("tenant_id", "base_currency", "quote_currency", "rate_date"), _build_fx, ("base_currency", "quote_currency", "rate_date")
    ),
}


def build_row(
    stream: str,
    record: dict[str, Any],
    ctx: MappingContext,
    *,
    raw_id: int,
    source_updated_at: datetime | None,
) -> CanonicalRow:
    from .commerce import REVISION_STREAMS,build_values,IDENTITIES
    if stream in REVISION_STREAMS:
        try:
            values=build_values(stream,record,ctx)
        except (ValueError, KeyError) as exc:
            from .streams import FieldError
            raise FieldError(stream, str(exc)) from exc
        return CanonicalRow(stream,tuple(values[k] for k in (*IDENTITIES[stream],'revision')),values,raw_id,source_updated_at)
    writer = WRITERS[stream]
    values = writer.build(record, ctx)
    key = tuple(values[column] for column in writer.key_columns)
    return CanonicalRow(stream, key, values, raw_id, source_updated_at)


def _dedupe(rows: Sequence[CanonicalRow]) -> list[CanonicalRow]:
    """One row per conflict key (Postgres rejects ON CONFLICT touching the same row twice).

    The newest source version wins; ties are broken by landing order (raw id).
    """

    floor = datetime.min.replace(tzinfo=UTC)
    best: dict[tuple, CanonicalRow] = {}
    for row in rows:
        current = best.get(row.key)
        rank = (row.source_updated_at or floor, row.raw_id)
        if current is None or rank >= (current.source_updated_at or floor, current.raw_id):
            best[row.key] = row
    return list(best.values())


def upsert_rows(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    source_system: str,
    stream: str,
    rows: Sequence[CanonicalRow],
    ctx: MappingContext,
) -> UpsertResult:
    from .commerce import REVISION_STREAMS
    if stream in ('market_signals',*REVISION_STREAMS):
        raise ValueError('market revisions must use append_revision with server availability metadata')
    if not rows:
        return UpsertResult(0, [])
    writer = WRITERS[stream]
    table = writer.table.__table__
    unique_rows = _dedupe(rows)

    previous_order_days: dict[tuple, tuple[uuid.UUID, date]] = {}
    if stream == "order_lines":
        previous_order_days = _existing_order_days(session, tenant_id, source_system, unique_rows, ctx)

    values: list[dict[str, Any]] = []
    for row in unique_rows:
        item = dict(row.values)
        item["tenant_id"] = tenant_id
        if "id" in table.c and stream != "fx_rates":
            item["id"] = uuid.uuid4()
        if "source_system" in table.c:
            item["source_system"] = source_system
        if writer.hashed:
            item["content_hash"] = _hash(row.values)
        if writer.versioned:
            item["source_updated_at"] = row.source_updated_at
        values.append(item)

    statement = insert(table).values(values)
    excluded = statement.excluded
    immutable = set(writer.conflict) | {"id", "tenant_id", "created_at", "ingested_at"}
    update_set = {name: excluded[name] for name in values[0] if name not in immutable}
    if "updated_at" in table.c:
        update_set["updated_at"] = func.now()

    guards = []
    if writer.versioned:
        guards.append(
            or_(
                table.c.source_updated_at.is_(None),
                excluded.source_updated_at.is_(None),
                table.c.source_updated_at <= excluded.source_updated_at,
            )
        )
    if writer.hashed:
        guards.append(table.c.content_hash.is_distinct_from(excluded.content_hash))
    else:
        changed_columns = [name for name in update_set if name not in ("updated_at", "origin", "source_system")]
        guards.append(or_(*[table.c[name].is_distinct_from(excluded[name]) for name in changed_columns]))
    if writer.extra_guard is not None:
        guards.append(writer.extra_guard(table, excluded))

    statement = statement.on_conflict_do_update(
        index_elements=list(writer.conflict), set_=update_set, where=and_(*guards)
    ).returning(*[table.c[name] for name in writer.key_columns])
    changed_keys = [tuple(result) for result in session.execute(statement).all()]

    if writer.versioned and writer.hashed:
        # No business change/outbox event, but advance the source watermark even when
        # the payload is identical. Otherwise an intermediate older change can win later.
        metadata_update = insert(table).values(values)
        session.execute(metadata_update.on_conflict_do_update(
            index_elements=list(writer.conflict),
            set_={"source_updated_at": metadata_update.excluded.source_updated_at},
            where=and_(table.c.content_hash == metadata_update.excluded.content_hash,
                       or_(table.c.source_updated_at.is_(None),
                           table.c.source_updated_at < metadata_update.excluded.source_updated_at)),
        ))

    rebuilt = 0
    if stream == "order_lines" and changed_keys:
        changed = set(changed_keys)
        days = {previous_order_days[key] for key in changed if key in previous_order_days}
        days |= {
            (row.values["location_id"], _local_date(row.values["ordered_at"], ctx, row.values["location_id"]))
            for row in unique_rows
            if row.key in changed
        }
        rebuilt = rebuild_daily_demand(session, tenant_id, days)
    return UpsertResult(len(changed_keys), changed_keys, rebuilt)


def _local_date(moment: datetime, ctx: MappingContext, location_id: uuid.UUID) -> date:
    return moment.astimezone(ZoneInfo(ctx.location_timezones.get(location_id) or "UTC")).date()


def _existing_order_days(
    session: Session,
    tenant_id: uuid.UUID,
    source_system: str,
    rows: Sequence[CanonicalRow],
    ctx: MappingContext,
) -> dict[tuple, tuple[uuid.UUID, date]]:
    """Day an existing order line currently contributes to (it must be rebuilt if the line moves)."""

    keys = [row.key for row in rows]
    days: dict[tuple, tuple[uuid.UUID, date]] = {}
    for start in range(0, len(keys), 1000):
        chunk = keys[start : start + 1000]
        for order_id, line_id, location_id, ordered_at in session.execute(
            select(
                OrderLine.external_order_id, OrderLine.external_line_id, OrderLine.location_id, OrderLine.ordered_at
            ).where(
                OrderLine.tenant_id == tenant_id,
                OrderLine.source_system == source_system,
                tuple_(OrderLine.external_order_id, OrderLine.external_line_id).in_(chunk),
            )
        ):
            days[(order_id, line_id)] = (location_id, _local_date(ordered_at, ctx, location_id))
    return days


_DELETE_ORDER_DEMAND = text(
    """
    UPDATE daily_demand AS dd SET units = 0, updated_at = now()
    FROM unnest(CAST(:locations AS uuid[]), CAST(:days AS date[])) AS p(location_id, d)
    WHERE dd.tenant_id = :tenant AND dd.location_id = p.location_id AND dd.demand_date = p.d
      AND dd.origin = 'orders'
    """
)

_INSERT_ORDER_DEMAND = text(
    """
    WITH pairs AS (
        SELECT DISTINCT p.location_id, p.d,
               (p.d::timestamp AT TIME ZONE l.timezone) AS start_at,
               ((p.d + 1)::timestamp AT TIME ZONE l.timezone) AS end_at
        FROM unnest(CAST(:locations AS uuid[]), CAST(:days AS date[])) AS p(location_id, d)
        JOIN location AS l ON l.id = p.location_id AND l.tenant_id = :tenant
    )
    INSERT INTO daily_demand (tenant_id, sku_id, location_id, demand_date, units, stockout, origin, source_system, updated_at)
    SELECT :tenant, COALESCE(c.component_sku_id, o.sku_id), pairs.location_id, pairs.d,
           SUM(CASE WHEN o.demand_eligible
               THEN o.quantity * COALESCE(c.quantity, 1) ELSE 0 END), false, 'orders', 'orders', now()
    FROM pairs
    JOIN order_line AS o
      ON o.tenant_id = :tenant AND o.location_id = pairs.location_id
     AND o.ordered_at >= pairs.start_at AND o.ordered_at < pairs.end_at
    LEFT JOIN sku_component AS c ON c.tenant_id = :tenant AND c.parent_sku_id = o.sku_id
    GROUP BY 2, 3, 4
    ON CONFLICT (tenant_id, sku_id, location_id, demand_date) DO UPDATE
       SET units = excluded.units, stockout = daily_demand.stockout, origin = 'orders',
           source_system = 'orders', updated_at = now()
    """
)


_RESET_OBSERVATIONS = text("""
    UPDATE demand_observation dd SET units=0, updated_at=now()
    FROM unnest(CAST(:locations AS uuid[]), CAST(:days AS date[])) p(location_id,d)
    WHERE dd.tenant_id=:tenant AND dd.location_id=p.location_id AND dd.demand_date=p.d AND dd.kind='order_lines'
""")

_INSERT_OBSERVATIONS = text("""
    WITH pairs AS (
      SELECT DISTINCT p.location_id, p.d,
             (p.d::timestamp AT TIME ZONE l.timezone) start_at,
             ((p.d+1)::timestamp AT TIME ZONE l.timezone) end_at
      FROM unnest(CAST(:locations AS uuid[]), CAST(:days AS date[])) p(location_id,d)
      JOIN location l ON l.id=p.location_id AND l.tenant_id=:tenant
    )
    INSERT INTO demand_observation
      (id,tenant_id,source_system,kind,sku_id,location_id,channel,demand_date,units,state,updated_at)
    SELECT gen_random_uuid(), :tenant, o.source_system, 'order_lines', COALESCE(c.component_sku_id,o.sku_id),
           p.location_id, COALESCE(NULLIF(o.channel,''),'*'), p.d,
           SUM(CASE WHEN o.demand_eligible THEN o.quantity * COALESCE(c.quantity,1) ELSE 0 END), 'observed', now()
    FROM pairs p JOIN order_line o ON o.tenant_id=:tenant AND o.location_id=p.location_id
      AND o.ordered_at>=p.start_at AND o.ordered_at<p.end_at
    LEFT JOIN sku_component c ON c.tenant_id=:tenant AND c.parent_sku_id=o.sku_id
    GROUP BY o.source_system,5,6,7,8
    ON CONFLICT (tenant_id,source_system,kind,sku_id,location_id,channel,demand_date)
    DO UPDATE SET units=excluded.units, updated_at=now()
""")


def rebuild_daily_demand(session: Session, tenant_id: uuid.UUID, days: set[tuple[uuid.UUID, date]]) -> int:
    """Recompute order-derived demand for the affected (location, local day) pairs only.

    Bundles are exploded through ``sku_component``; cancelled lines are excluded. Days whose orders
    were all cancelled retain a zero row and their stockout marker.
    """

    if not days:
        return 0
    ordered = sorted(days)
    params = {
        "tenant": tenant_id,
        "locations": [location for location, _ in ordered],
        "days": [day for _, day in ordered],
    }
    session.execute(_RESET_OBSERVATIONS, params)
    session.execute(_INSERT_OBSERVATIONS, params)
    session.execute(_DELETE_ORDER_DEMAND, params)
    session.execute(_INSERT_ORDER_DEMAND, {**params, "cancelled": list(CANCELLED_ORDER_STATUSES)})
    return len(ordered)


def rebuild_all_daily_demand(session: Session, tenant_id: uuid.UUID, since: date | None = None) -> int:
    """Operational repair: rebuild every (location, day) that has order lines (optionally since a date)."""

    query = select(OrderLine.location_id, OrderLine.ordered_at, Location.timezone).join(
        Location, Location.id == OrderLine.location_id
    ).where(OrderLine.tenant_id == tenant_id)
    if since is not None:
        query = query.where(
            OrderLine.ordered_at >= datetime.combine(since - timedelta(days=1), time.min, tzinfo=UTC)
        )
    days = {
        (location_id, ordered_at.astimezone(ZoneInfo(timezone or "UTC")).date())
        for location_id, ordered_at, timezone in session.execute(query)
    }
    return rebuild_daily_demand(session, tenant_id, days)
