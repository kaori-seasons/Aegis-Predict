"""Tenant-scoped canonical tables -> immutable algorithm input, with data quality gates."""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import InvalidInput
from ..core.util import json_safe, utcnow
from ..models import (DailyDemand, InventorySnapshot, Lane, Location, PlanLinePin, PurchaseOrderLine,
                      QuarantineRecord, RawRecord, Sku, SupplierTerms, SyncStreamState, TransportOption)
from .policy import PlanningPolicy


@dataclass(frozen=True)
class AssembledNetwork:
    payload: dict
    ids: dict
    pins: list[dict]
    watermarks: dict
    freshness: dict
    stale: bool


def assemble(session: Session, tenant_id: uuid.UUID, cutoff: datetime, policy: PlanningPolicy) -> AssembledNetwork:
    if cutoff.tzinfo is None or cutoff > utcnow() + timedelta(seconds=5):
        raise InvalidInput("cutoff must be timezone-aware and cannot be in the future")
    if policy.inventory_source or policy.demand_source or policy.po_source:
        raise InvalidInput("configure source_authority per location/channel instead of legacy global source filters")
    skus = list(session.scalars(select(Sku).where(Sku.tenant_id == tenant_id, Sku.is_active).order_by(Sku.code)))
    locations = list(session.scalars(select(Location).where(Location.tenant_id == tenant_id, Location.is_active).order_by(Location.code)))
    pools = [l for l in locations if l.kind == "pool"]
    if not skus or len(pools) != 1:
        raise InvalidInput("planning requires active SKUs and exactly one stock pool")
    pool = pools[0]
    from ..integration.observations import authorities, latest_batch, load_demand, zero_is_proven
    from ..inventory.service import held_units, on_order_units
    from ..models import IntegrationConnection
    rules = authorities(session, tenant_id)
    by_scope = {(r.domain, r.location_id): r for r in rules if r.domain != "demand"}
    sources = {c.id: c.source_system for c in session.scalars(select(IntegrationConnection))}
    inventory_batches = {}
    for location in locations:
        rule = by_scope.get(("inventory", location.id))
        if rule is None:
            raise InvalidInput(f"inventory source authority missing: {location.code}")
        inventory_batches[location.id] = latest_batch(session, rule)
    demand_locations = {r.location_id for r in rules if r.domain == "demand"}
    if not demand_locations:
        raise InvalidInput("demand source authority must be configured")
    sku_by_id = {s.id: s for s in skus}
    location_by_id = {l.id: l for l in locations}
    warehouse_ids = {l.id for l in locations if l.kind != "pool"}
    query = select(InventorySnapshot).where(InventorySnapshot.tenant_id == tenant_id, InventorySnapshot.as_of <= cutoff)
    if policy.inventory_source:
        query = query.where(InventorySnapshot.source_system == policy.inventory_source)
    # Latest per source first. Multiple sources for one stock location must never be summed.
    ranked = query.add_columns(func.row_number().over(
        partition_by=(InventorySnapshot.sku_id, InventorySnapshot.location_id, InventorySnapshot.source_system),
        order_by=(InventorySnapshot.as_of.desc(), InventorySnapshot.ingested_at.desc())
    ).label("rank")).subquery()
    from sqlalchemy.orm import aliased
    latest = list(session.scalars(select(aliased(InventorySnapshot, ranked)).where(ranked.c.rank == 1)
                                  .order_by(ranked.c.sku_id, ranked.c.location_id, ranked.c.source_system)))
    snapshots = {}
    for s in latest:
        if s.sku_id not in sku_by_id or s.location_id not in location_by_id:
            continue
        rule = by_scope[("inventory", s.location_id)]
        if s.source_system != sources[rule.connection_id]:
            continue
        if s.ingestion_batch_id != inventory_batches[s.location_id].id:
            raise InvalidInput("inventory coverage is incomplete; supply explicit zero rows for absent stock",
                               code="incomplete_inventory_snapshot")
        key = (s.sku_id, s.location_id)
        if key in snapshots:
            raise InvalidInput("multiple inventory sources for one SKU/location; select inventory_source")
        snapshots[key] = s
    demand, coverage = load_demand(session, tenant_id, rules, cutoff.date()-timedelta(days=policy.history_days+2))
    for key in list(demand):
        if key[0] not in sku_by_id or key[1] not in warehouse_ids:
            del demand[key]
            continue
        end = cutoff.astimezone(ZoneInfo(location_by_id[key[1]].timezone)).date()-timedelta(days=1)
        demand[key] = {day: row for day, row in demand[key].items() if day <= end}
        if not demand[key]:
            del demand[key]
    if not demand:
        raise InvalidInput("no completed local demand days before cutoff")
    issues: list[str] = []
    pois = defaultdict(list)
    po_query = select(PurchaseOrderLine).where(PurchaseOrderLine.tenant_id == tenant_id,
                                             PurchaseOrderLine.status.in_(("open", "approved", "ordered", "partial", "in_transit", "shipped")))
    if policy.po_source:
        po_query = po_query.where(PurchaseOrderLine.source_system == policy.po_source)
    po_sources = defaultdict(set)
    for po in session.scalars(po_query):
        key = (po.sku_id, po.location_id)
        rule = by_scope.get(("inbound", po.location_id))
        if rule is None:
            raise InvalidInput("open purchase orders require an explicit inbound source authority")
        latest_batch(session, rule)
        if po.source_system != sources[rule.connection_id]:
            continue
        po_sources[key].add(po.source_system)
        if po.qty_received > po.qty_ordered:
            raise InvalidInput("purchase order receipt exceeds ordered quantity")
        pois[key].append(po)
    if any(len(sources) > 1 for sources in po_sources.values()):
        raise InvalidInput("multiple PO sources; select po_source to prevent duplicate inbound")
    history, inventory, demand_marks = [], [], {}
    for key in sorted(demand, key=lambda k: (sku_by_id[k[0]].code, location_by_id[k[1]].code)):
        sku_id, loc_id = key
        sku, loc = sku_by_id[sku_id], location_by_id[loc_id]
        snap = snapshots.get(key)
        if snap is None:
            raise InvalidInput(f"missing inventory snapshot: {sku.code}/{loc.code}")
        age = (cutoff - snap.as_of).total_seconds() / 3600
        if age > policy.max_inventory_age_hours:
            issues.append(f"inventory stale: {sku.code}/{loc.code} ({age:.1f}h)")
        end = cutoff.astimezone(ZoneInfo(loc.timezone)).date() - timedelta(days=1)
        days = demand[key]
        start = max(min(days), end - timedelta(days=policy.history_days-1))
        length = (end-start).days + 1
        calendar = [start + timedelta(days=i) for i in range(length)]
        from ..integration.observations import zero_is_proven
        tz = ZoneInfo(loc.timezone)
        expected = coverage[loc_id]
        missing, flags, units = [], [], []
        for day in calendar:
            observed = days.get(day, {"units": 0, "censored": False, "missing": False, "rules": set()})
            unknown = observed["missing"] or any(
                not zero_is_proven(rule, intervals, day, tz) if rule.stream == "order_lines"
                else rule.id not in observed["rules"]
                for rule, intervals in expected)
            missing.append(unknown)
            flags.append(observed["censored"])
            units.append(observed["units"])
        if sum(not (m or f) for m, f in zip(missing, flags)) < 7:
            issues.append(f"insufficient observed demand: {sku.code}/{loc.code}")
        if sum(missing)/max(1,len(missing)) > policy.max_missing_demand_ratio:
            raise InvalidInput("missing demand coverage exceeds policy; missing days are not zero demand",
                               code="missing_demand_coverage")
        if (end - max(days)).days >= policy.max_demand_age_days:
            issues.append(f"demand stale: {sku.code}/{loc.code}")
        inbound_rule = by_scope.get(("inbound", loc_id))
        on_order = on_order_units(session, snap, pois[key], inbound_rule.inbound_mode if inbound_rule else "linked")
        inventory.append({"sku": sku.code, "warehouse_id": loc.code, "on_hand": snap.on_hand,
                          "in_transit": snap.in_transit, "allocated": snap.allocated, "backorder": snap.backorder,
                          "on_order": on_order})
        history.append({"sku": sku.code, "warehouse_id": loc.code, "daily_units": units, "stockout_days": flags, "missing_days": missing})
        demand_marks[f"{sku.code}/{loc.code}"] = {"first": start, "last": max(days), "rows": len(days)}
    # Every stock-bearing destination must have demand coverage, or the plan would silently omit it.
    for key, snap in snapshots.items():
        if key[1] in warehouse_ids and key not in demand and (snap.on_hand or snap.in_transit or snap.allocated):
            raise InvalidInput(f"missing demand authority/coverage for stocked SKU: {sku_by_id[key[0]].code}/{location_by_id[key[1]].code}")
    supply = []
    for sku in skus:
        snap = snapshots.get((sku.id, pool.id))
        if snap is None:
            raise InvalidInput(f"missing pool snapshot: {sku.code}")
        if (cutoff - snap.as_of).total_seconds()/3600 > policy.max_inventory_age_hours:
            issues.append(f"pool inventory stale: {sku.code}")
        supply.append({"sku": sku.code, "available_units": snap.on_hand, "reserved_units": snap.allocated + snap.backorder + held_units(session, tenant_id, sku.id, pool.id)})
    lanes, transports = [], []
    for lane in session.scalars(select(Lane).where(Lane.tenant_id == tenant_id).order_by(Lane.id)):
        if lane.from_location_id == pool.id and lane.to_location_id in warehouse_ids:
            lanes.append({"from": "GLOBAL-POOL", "to": location_by_id[lane.to_location_id].code,
                          "lead_time_days": lane.lead_time_days, "lead_time_std_days": lane.lead_time_std_days or 0,
                          "unit_cost": lane.unit_cost})
    for opt in session.scalars(select(TransportOption).where(TransportOption.tenant_id == tenant_id).order_by(TransportOption.id)):
        if opt.from_location_id == pool.id and opt.to_location_id in warehouse_ids:
            transports.append({"from": "GLOBAL-POOL", "to": location_by_id[opt.to_location_id].code,
                               "mode": opt.mode, "lead_time_days": opt.lead_time_days, "unit_cost": opt.unit_cost,
                               "reliability": opt.reliability, "max_units": opt.max_units})
    terms, seen_terms = [], set()
    for t in session.scalars(select(SupplierTerms).where(SupplierTerms.tenant_id == tenant_id).order_by(SupplierTerms.id)):
        if t.sku_id in sku_by_id:
            if t.sku_id in seen_terms:
                raise InvalidInput("multiple supplier terms for a SKU; consolidate a dispatch quota before planning")
            seen_terms.add(t.sku_id)
            terms.append({"sku": sku_by_id[t.sku_id].code, "supplier": t.supplier, "available_units": t.available_units,
                          "min_order_qty": t.min_order_qty, "unit_purchase_cost": t.unit_purchase_cost, "capital_budget": t.capital_budget})
    recent = cutoff - timedelta(days=1)
    raw_count = session.scalar(select(func.count()).select_from(RawRecord).where(RawRecord.tenant_id == tenant_id, RawRecord.received_at >= recent)) or 0
    bad_count = session.scalar(select(func.count()).select_from(RawRecord).where(RawRecord.tenant_id == tenant_id,
                               RawRecord.received_at >= recent, RawRecord.normalize_status == "quarantined")) or 0
    pending = session.scalar(select(func.count()).select_from(RawRecord).where(RawRecord.tenant_id == tenant_id, RawRecord.normalize_status == "pending")) or 0
    if bad_count / max(1, raw_count) > policy.max_quarantine_ratio:
        issues.append("quarantine ratio exceeds policy")
    if pending:
        issues.append("normalization backlog remains")
    if issues and not policy.allow_stale_data:
        raise InvalidInput("planning data quality gate failed", code="data_quality_gate", details={"issues": issues})
    pins = []
    for p in session.scalars(select(PlanLinePin).where(PlanLinePin.tenant_id == tenant_id, PlanLinePin.is_active)):
        if p.expires_at is None or p.expires_at > cutoff:
            if p.sku_id not in sku_by_id or p.location_id not in location_by_id:
                raise InvalidInput("active pin references inactive master data")
            pins.append({"sku": sku_by_id[p.sku_id].code, "warehouse_id": location_by_id[p.location_id].code,
                         "units": p.units, "transport_mode": p.transport_mode})
    warehouses = [l for l in locations if l.id in warehouse_ids]
    payload = {"service_level_z": policy.service_level_z,
               "skus": [{"sku": s.code, "name": s.name, "category": s.category, "pack_size": s.pack_size,
                         "unit_margin": s.unit_margin, "service_class": s.service_class} for s in skus],
               "warehouses": [{"warehouse_id": l.code, "region": l.region, "country": l.country,
                               "handling_capacity_per_day": l.handling_capacity_per_day, "last_mile_score": l.last_mile_score,
                               "latitude": l.latitude, "longitude": l.longitude} for l in warehouses],
               "lanes": lanes, "transport_options": transports, "inventory": inventory, "demand_history": history,
               "global_supply_pool": supply, "market_signals": [], "procurement_constraints": terms,
               "warehouse_constraints": [{"warehouse_id": l.code, "capacity_units": l.capacity_units,
                                           "storage_cost_per_unit_day": l.storage_cost_per_unit_day or 0,
                                           "max_daily_inbound_units": l.max_daily_inbound_units if l.max_daily_inbound_units is not None else l.handling_capacity_per_day}
                                          for l in warehouses if l.capacity_units is not None]}
    marks = {"authorities": [{"id": str(r.id), "domain": r.domain, "location_id": str(r.location_id),
               "channel": r.channel, "connection_id": str(r.connection_id), "stream": r.stream,
               "inbound_mode": r.inbound_mode, "coverage_start": r.coverage_start}
              for r in sorted(rules, key=lambda r: str(r.id))],
             "inventory_batches": {str(k): str(v.id) for k,v in inventory_batches.items()}, "inventory": [str(s.id) for s in snapshots.values()], "demand": demand_marks,
             "streams": [{"connection_id": s.connection_id, "stream": s.stream, "cursor": s.cursor,
                          "last_success_at": s.last_success_at} for s in session.scalars(select(SyncStreamState).where(SyncStreamState.tenant_id == tenant_id))]}
    return AssembledNetwork(payload, {"pool": str(pool.id), "skus": {s.code: str(s.id) for s in skus},
                                    "locations": {l.code: str(l.id) for l in locations}}, pins,
                            json_safe(marks), {"issues": issues, "quarantine_ratio": bad_count/max(1, raw_count)}, bool(issues))
