"""Operator-only bootstrap and sample import through the actual raw/normalize pipeline."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select
from overseas_inventory.models import load_network
from .core.db import admin_session, tenant_session
from .core.errors import Conflict
from .core.security import hash_password
from .core.tenancy import system_principal
from .core.util import utcnow
from .integration.connections import create_connection
from .integration.normalize import normalize_batch
from .integration.raw import land_batch
from .integration.spi import RawItem
from .models import AppUser, IntegrationConnection, Tenant
from .planning.lifecycle import create_policy


def bootstrap_tenant(slug: str, name: str, email: str, password: str) -> uuid.UUID:
    password_hash = hash_password(password)
    with admin_session() as session:
        if session.scalar(select(Tenant.id).where(Tenant.slug == slug)):
            raise Conflict("tenant slug already exists")
        tenant = Tenant(id=uuid.uuid4(), slug=slug, name=name, timezone="UTC")
        session.add(tenant); session.flush()
        session.add(AppUser(tenant_id=tenant.id, email=email.strip().lower(), password_hash=password_hash, roles=["TENANT_ADMIN"]))
        tenant_id = tenant.id
    with tenant_session(tenant_id) as session:
        create_policy(session, system_principal(tenant_id, "bootstrap"), {})
    return tenant_id


def import_sample(tenant_id: uuid.UUID, path: Path) -> dict:
    network = load_network(path)
    principal = system_principal(tenant_id, "sample-import")
    now = utcnow()
    rows = []
    def add(stream, external_id, payload):
        rows.append(RawItem(stream, external_id, payload, now))
    for s in network.skus.values():
        add("skus", s.sku, {"sku": s.sku, "name": s.name, "category": s.category, "pack_size": s.pack_size,
                            "unit_margin": s.unit_margin, "service_class": s.service_class})
    add("locations", "GLOBAL-POOL", {"code": "GLOBAL-POOL", "name": "Global pool", "kind": "pool", "timezone": "UTC"})
    for w in network.warehouses.values():
        c = network.warehouse_constraints.get(w.warehouse_id)
        add("locations", w.warehouse_id, {"code": w.warehouse_id, "kind": "warehouse", "name": w.warehouse_id,
             "region": w.region, "country": w.country, "timezone": "UTC", "latitude": w.latitude, "longitude": w.longitude,
             "last_mile_score": w.last_mile_score, "handling_capacity_per_day": w.handling_capacity_per_day,
             "capacity_units": c.capacity_units if c else 1000000, "storage_cost_per_unit_day": c.storage_cost_per_unit_day if c else 0.02,
             "max_daily_inbound_units": c.max_daily_inbound_units if c else w.handling_capacity_per_day})
    for l in network.lanes:
        add("lanes", f"{l.source}|{l.target}", {"from": l.source, "to": l.target, "lead_time_days": l.lead_time_days,
                                              "lead_time_std_days": l.lead_time_std_days, "unit_cost": l.unit_cost})
    for o in network.transport_options:
        add("transport_options", f"{o.source}|{o.target}|{o.mode}", {"from": o.source, "to": o.target, "mode": o.mode,
            "lead_time_days": o.lead_time_days, "unit_cost": o.unit_cost, "reliability": o.reliability, "max_units": o.max_units})
    for i in network.inventory.values():
        add("inventory_snapshots", f"{i.sku}|{i.warehouse_id}|{now.isoformat()}", {"sku": i.sku, "location": i.warehouse_id,
            "as_of": now.isoformat(), "on_hand": i.on_hand, "in_transit": i.in_transit, "allocated": i.allocated,
            "backorder": i.backorder})
    for s in network.global_supply_pool.values():
        add("inventory_snapshots", f"{s.sku}|GLOBAL-POOL|{now.isoformat()}", {"sku": s.sku, "location": "GLOBAL-POOL",
            "as_of": now.isoformat(), "on_hand": s.available_units, "allocated": s.reserved_units})
    for s in network.demand_history:
        for index, units in enumerate(s.daily_units):
            day = (now.date() - timedelta(days=len(s.daily_units) - index)).isoformat()
            add("daily_demand", f"{s.sku}|{s.warehouse_id}|{day}", {"sku": s.sku, "location": s.warehouse_id,
                                                                  "date": day, "units": units, "stockout": False})
    for t in network.procurement_constraints.values():
        add("supplier_terms", f"{t.sku}|{t.supplier}", {"sku": t.sku, "supplier": t.supplier,
            "available_units": t.available_units, "min_order_qty": t.min_order_qty,
            "unit_purchase_cost": t.unit_purchase_cost, "capital_budget": t.capital_budget})
    with tenant_session(tenant_id) as session:
        connection = session.scalar(select(IntegrationConnection).where(IntegrationConnection.name == "sample-import"))
        if connection is None:
            connection = create_connection(session, principal, name="sample-import", kind="file_csv",
                                           config={"directory": str(tenant_id)}, source_system="sample-import")
        # This CLI can never certify synthetic fixture facts as live stock, even in a live deployment.
        connection.is_mock = True
        connection_id = connection.id
    landed = 0
    from .integration.batches import start_batch, finish_landing
    from .integration.streams import ordered_streams
    sample_batches = {}
    for stream in ordered_streams({r.stream for r in rows}):
        selected = [r for r in rows if r.stream == stream]
        with tenant_session(tenant_id) as session:
            batch = start_batch(session, tenant_id, connection_id, stream)
            batch_id = batch.id
            sample_batches[stream] = batch_id
        for start in range(0, len(selected), 500):
            with tenant_session(tenant_id) as session:
                stats = land_batch(session, tenant_id=tenant_id, connection_id=connection_id,
                                   sync_run_id=None, items=selected[start:start+500], batch_id=batch_id)
                landed += stats.landed
        with tenant_session(tenant_id) as session:
            finish_landing(session, batch_id)
    normalized = quarantined = 0
    while True:
        with tenant_session(tenant_id) as session:
            stats = normalize_batch(session, tenant_id, connection_id=connection_id, limit=1000)
        normalized += stats.normalized; quarantined += stats.quarantined
        if not stats.processed:
            break
    # The source network is an independent synthetic fixture: compare it with landed projections.
    from .integration.reconciliation import reconcile
    for stream, batch_id in sample_batches.items():
        with tenant_session(tenant_id) as session:
            reconcile(session, principal, batch_id,
                source_rows=[{"external_id": r.external_id, "payload": r.payload, "source_updated_at": r.source_updated_at}
                             for r in rows if r.stream == stream], scope={},
                evidence="independent synthetic network fixture; never real merchant evidence")
    from .integration.authority import set_authority
    from .models import Location
    with tenant_session(tenant_id) as session:
        for location in session.scalars(select(Location).where(Location.is_active)):
            set_authority(session, principal, domain="inventory", location_id=location.id,
                          connection_id=connection_id, stream="inventory_snapshots", note="sample import: synthetic inventory only")
            if location.kind != "pool":
                set_authority(session, principal, domain="demand", location_id=location.id,
                              connection_id=connection_id, stream="daily_demand", note="sample import: synthetic daily totals only")
    return {"landed": landed, "normalized": normalized, "quarantined": quarantined}
