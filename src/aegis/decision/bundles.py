"""Published single-level BOM versions, event-time explosion and conserved kit capacity."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from math import floor
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from ..core.util import json_safe, utcnow
from ..models import (
    DecisionArtifact,
    DecisionCase,
    DemandObservation,
    IntegrationConnection,
    InventoryInboundLink,
    InventorySnapshot,
    Location,
    OrderLine,
    PurchaseOrderLine,
    Sku,
    SourceAuthority,
)
from .history import artifact


def lock(session, tenant_id):
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": tenant_id.int % (2**63 - 1)}
    )


def case_for_update(session, ident, kind, revision):
    row = session.get(DecisionCase, ident, with_for_update=True)
    if row is None or row.kind != kind:
        raise NotFound("workflow not found")
    if row.revision != revision:
        raise Conflict("workflow changed; refresh before editing")
    return row


def separate_approver(row, principal):
    if (
        principal.user_id is None
        or row.created_by is None
        or principal.user_id == row.created_by
    ):
        raise PermissionDenied("approval requires a different authenticated member")


def components(session, parent, edges):
    codes = {r.code: r for r in session.scalars(select(Sku).where(Sku.is_active))}
    if parent not in codes or not edges:
        raise InvalidInput("active parent and nonempty components required")
    if len(edges) > 100 or len({e["sku"] for e in edges}) != len(edges):
        raise InvalidInput("duplicate components or BOM size limit")
    for e in edges:
        if (
            e["sku"] not in codes
            or e["sku"] == parent
            or type(e["quantity"]) is not int
            or not 1 <= e["quantity"] <= 100000
        ):
            raise InvalidInput(
                "BOM requires positive integer quantities and distinct active component SKUs"
            )
    published = [
        v.payload
        for v in session.scalars(
            select(DecisionArtifact).where(DecisionArtifact.kind == "bom")
        )
    ]
    from ..models import SkuComponent

    catalog = {r.id: r.code for r in codes.values()}
    old = list(session.scalars(select(SkuComponent)))
    parents = {v["parent"] for v in published} | {
        catalog[e.parent_sku_id] for e in old if e.parent_sku_id in catalog
    }
    used = {e["sku"] for v in published for e in v["components"]}
    used |= {catalog[e.component_sku_id] for e in old if e.component_sku_id in catalog}
    if parent in used or any(e["sku"] in parents for e in edges):
        raise InvalidInput(
            "nested/cyclic BOM is unsupported; flatten and approve explicit base-unit quantities"
        )
    return sorted(edges, key=lambda e: e["sku"])


def draft(session, p, parent, edges, effective_from, stock_mode, note):
    if stock_mode not in ("virtual", "prepacked") or not note.strip():
        raise InvalidInput("stock mode and reason required")
    lock(session, p.tenant_id)
    edges = components(session, parent, edges)
    row = DecisionCase(
        tenant_id=p.tenant_id,
        kind="bom_draft",
        scope_key=uuid.uuid4().hex,
        status="draft",
        created_by=p.user_id,
        payload=json_safe(
            {
                "parent": parent,
                "components": edges,
                "effective_from": effective_from,
                "stock_mode": stock_mode,
                "note": note,
            }
        ),
    )
    session.add(row)
    session.flush()
    record_audit(session, p, "bom.draft", "decision_case", row.id, after=row.payload)
    return row


def publish(session, p, ident, revision, note):
    lock(session, p.tenant_id)
    row = case_for_update(session, ident, "bom_draft", revision)
    if row.status != "draft":
        raise Conflict("BOM draft already decided")
    separate_approver(row, p)
    if not note.strip():
        raise InvalidInput("approval reason required")
    data = dict(row.payload)
    components(session, data["parent"], data["components"])
    versions = list(
        session.scalars(select(DecisionArtifact).where(DecisionArtifact.kind == "bom"))
    )
    if any(
        v.payload["parent"] == data["parent"]
        and v.payload["effective_from"] == data["effective_from"]
        for v in versions
    ):
        raise Conflict("BOM effective date already published; choose a distinct date")
    data["version"] = 1 + sum(v.payload["parent"] == data["parent"] for v in versions)
    data["published_at"] = utcnow()
    data["approval_note"] = note
    version = artifact(
        session,
        p.tenant_id,
        "bom",
        f"{data['parent']}:{data['version']}",
        data,
        actor=p.user_id,
    )
    row.status = "published"
    row.approved_by = p.user_id
    row.artifact_id = version.id
    row.revision += 1
    record_audit(
        session,
        p,
        "bom.publish",
        "decision_case",
        row.id,
        after={"version_id": version.id, "note": note},
    )
    return row


def select_bom(versions, parent, day):
    choices = [
        v
        for v in versions
        if v["parent"] == parent and date.fromisoformat(v["effective_from"]) <= day
    ]
    return (
        max(choices, key=lambda v: (v["effective_from"], v["version"]))
        if choices
        else None
    )


def rebuild_order_demand(session, cp, bom_facts):
    versions = [v["payload"] for v in bom_facts if v["kind"] == "bom"]
    skus = {str(s.id): s.code for s in session.scalars(select(Sku))}
    ids = {code: ident for ident, code in skus.items()}
    locations = {r.id: r for r in session.scalars(select(Location))}
    from ..models import SkuComponent

    legacy = defaultdict(list)
    for edge in session.scalars(select(SkuComponent)):
        if edge.quantity <= 0:
            raise InvalidInput("invalid legacy BOM quantity")
        legacy[skus[str(edge.parent_sku_id)]].append(
            {"sku": skus[str(edge.component_sku_id)], "quantity": edge.quantity}
        )
    if any(e["sku"] in legacy for edges in legacy.values() for e in edges):
        raise InvalidInput(
            "nested legacy BOM must be flattened before point-in-time forecasting"
        )
    grouped = defaultdict(int)
    for order in session.scalars(select(OrderLine)):
        if not order.demand_eligible or order.ordered_at > cp.captured_at:
            continue
        loc = locations.get(order.location_id)
        if loc is None:
            continue
        day = order.ordered_at.astimezone(ZoneInfo(loc.timezone)).date()
        parent = skus[str(order.sku_id)]
        version = select_bom(versions, parent, day)
        edges = (
            version["components"]
            if version and version["stock_mode"] == "virtual"
            else (
                [{"sku": parent, "quantity": 1}]
                if version
                else legacy.get(parent, [{"sku": parent, "quantity": 1}])
            )
        )
        for edge in edges:
            grouped[
                (
                    order.source_system,
                    ids[edge["sku"]],
                    str(order.location_id),
                    order.channel,
                    day,
                )
            ] += order.quantity * edge["quantity"]
    session.execute(
        text("DELETE FROM pg_temp.demand_observation WHERE kind='order_lines'")
    )
    rows = [
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(cp.tenant_id),
            "kind": "order_lines",
            "source_system": key[0],
            "sku_id": key[1],
            "location_id": key[2],
            "channel": key[3],
            "demand_date": str(key[4]),
            "units": units,
            "state": "observed",
            "updated_at": cp.captured_at.isoformat(),
        }
        for key, units in sorted(grouped.items())
    ]
    if rows:
        import json

        session.execute(
            text(
                "INSERT INTO pg_temp.demand_observation SELECT * FROM jsonb_populate_recordset(NULL::public.demand_observation, CAST(:rows AS jsonb))"
            ),
            {"rows": json.dumps(rows)},
        )
    # Daily aggregates declare their quantity basis at source-authority level.
    # Base-unit feeds are never exploded a second time.
    sources = {
        c.id: c.source_system for c in session.scalars(select(IntegrationConnection))
    }
    rules = list(
        session.scalars(
            select(SourceAuthority).where(
                SourceAuthority.domain == "demand",
                SourceAuthority.stream == "daily_demand",
            )
        )
    )
    daily = []
    for obs in session.scalars(
        select(DemandObservation).where(DemandObservation.kind == "daily_demand")
    ):
        rule = next(
            (
                r
                for r in rules
                if r.location_id == obs.location_id
                and sources.get(r.connection_id) == obs.source_system
                and r.channel in ("*", obs.channel)
            ),
            None,
        )
        parent = skus[str(obs.sku_id)]
        if rule is None or rule.quantity_basis != "sellable_units":
            edges = [{"sku": parent, "quantity": 1}]
        else:
            version = select_bom(versions, parent, obs.demand_date)
            edges = (
                version["components"]
                if version and version["stock_mode"] == "virtual"
                else (
                    [{"sku": parent, "quantity": 1}]
                    if version
                    else legacy.get(parent, [{"sku": parent, "quantity": 1}])
                )
            )
        for edge in edges:
            daily.append(
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(cp.tenant_id),
                    "kind": "daily_demand",
                    "source_system": obs.source_system,
                    "sku_id": ids[edge["sku"]],
                    "location_id": str(obs.location_id),
                    "channel": obs.channel,
                    "demand_date": str(obs.demand_date),
                    "units": obs.units * edge["quantity"],
                    "state": obs.state,
                    "updated_at": cp.captured_at.isoformat(),
                }
            )
    session.execute(
        text("DELETE FROM pg_temp.demand_observation WHERE kind='daily_demand'")
    )
    if daily:
        import json

        session.execute(
            text(
                "INSERT INTO pg_temp.demand_observation SELECT * FROM jsonb_populate_recordset(NULL::public.demand_observation, CAST(:rows AS jsonb))"
            ),
            {"rows": json.dumps(daily)},
        )


def inbound_timeline(session, cp, payload, ids):
    """PO remaining is counted once; linked transit adopts its document's ETA.

    Unattributed transit and overdue/undated arrivals remain UNKNOWN, never day-zero stock.
    """
    sources = {
        c.id: c.source_system for c in session.scalars(select(IntegrationConnection))
    }
    rules = {
        r.location_id: r
        for r in session.scalars(
            select(SourceAuthority).where(SourceAuthority.domain == "inbound")
        )
    }
    orders = defaultdict(list)
    for po in session.scalars(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.status.in_(
                ("open", "approved", "ordered", "partial", "in_transit", "shipped")
            )
        )
    ):
        rule = rules.get(po.location_id)
        if rule and po.source_system == sources.get(rule.connection_id):
            orders[(str(po.sku_id), str(po.location_id))].append(po)
    result = {}
    for inv in payload["inventory"]:
        key = (ids["skus"][inv["sku"]], ids["locations"][inv["warehouse_id"]])
        entries = []
        unknown = 0
        transit = inv["in_transit"]
        pos = orders[key]
        rule = rules.get(uuid.UUID(key[1]))
        for po in pos:
            units = po.qty_ordered - po.qty_received
            loc = session.get(Location, po.location_id)
            today = cp.captured_at.astimezone(ZoneInfo(loc.timezone)).date()
            days = (
                (po.expected_arrival - today).days + 2 if po.expected_arrival else None
            )
            if days is None or po.expected_arrival < today:
                unknown += units
            else:
                entries.append({"day": days, "units": units, "po_id": str(po.id)})
        if transit:
            snap = session.scalar(
                select(InventorySnapshot)
                .where(
                    InventorySnapshot.sku_id == uuid.UUID(key[0]),
                    InventorySnapshot.location_id == uuid.UUID(key[1]),
                    InventorySnapshot.as_of <= cp.captured_at,
                )
                .order_by(InventorySnapshot.as_of.desc())
                .limit(1)
            )
            links = (
                list(
                    session.scalars(
                        select(InventoryInboundLink).where(
                            InventoryInboundLink.snapshot_id == snap.id
                        )
                    )
                )
                if snap
                else []
            )
            linked = sum(
                r.units
                for r in links
                if any(p.id == r.purchase_order_line_id for p in pos)
            )
            # Disjoint transit has no ETA proof in this schema; leave it unknown.
            unknown += (
                transit
                if rule is None or rule.inbound_mode == "disjoint"
                else max(0, transit - linked)
            )
        result[f"{inv['sku']}/{inv['warehouse_id']}"] = {
            "arrivals": entries,
            "unknown_inbound_units": unknown,
        }
    return result


def trace(session, p, checkpoint_id, sku, stock=None, allocations=None):
    from .history import facts, require_checkpoint

    cp = require_checkpoint(session, checkpoint_id)
    versions = [
        r["payload"]
        for r, _ in facts(session, cp, "decision_artifact")
        if r["kind"] == "bom"
    ]
    day = cp.captured_at.date()
    parents = {v["parent"] for v in versions}
    active = {k: select_bom(versions, k, day) for k in parents}
    active = {k: v for k, v in active.items() if v}
    forward = active.get(sku)
    reverse = [
        {
            "parent": k,
            "version": v["version"],
            "quantity": e["quantity"],
            "stock_mode": v["stock_mode"],
        }
        for k, v in active.items()
        for e in v["components"]
        if e["sku"] == sku
    ]
    # Stock preview is supplied only by the server from a verified PIT input.
    capacities = {}
    usage = defaultdict(int)
    if stock is not None:
        for parent, v in active.items():
            capacities[parent] = (
                min(
                    (
                        floor(max(0, stock.get(e["sku"], 0)) / e["quantity"])
                        for e in v["components"]
                    ),
                    default=0,
                )
                if v["stock_mode"] == "virtual"
                else max(0, stock.get(parent, 0))
            )
        for parent, units in (allocations or {}).items():
            if type(units) is not int or units < 0 or parent not in active:
                raise InvalidInput("invalid kit allocation")
            v = active[parent]
            edges = (
                v["components"]
                if v["stock_mode"] == "virtual"
                else [{"sku": parent, "quantity": 1}]
            )
            for e in edges:
                usage[e["sku"]] += units * e["quantity"]
        if any(n > max(0, stock.get(k, 0)) for k, n in usage.items()):
            raise Conflict(
                "shared component capacity exceeded; independent kit capacities cannot be added"
            )
    return {
        "checkpoint_id": str(cp.id),
        "sku": sku,
        "forward": forward,
        "where_used": reverse,
        "standalone_capacity": capacities,
        "component_usage": dict(usage),
        "capacity_semantics": "individual upper bounds; joint allocations consume shared components once",
    }


def validate_stock_shape(session, cp, payload):
    from .history import facts

    versions = [r["payload"] for r, _ in facts(session, cp, "decision_artifact")]
    locations = {r.code: r for r in session.scalars(select(Location))}
    for inv in payload["inventory"]:
        day = cp.captured_at.astimezone(
            ZoneInfo(locations[inv["warehouse_id"]].timezone)
        ).date()
        v = select_bom(versions, inv["sku"], day)
        if (
            v
            and v["stock_mode"] == "virtual"
            and any(
                inv.get(k, 0)
                for k in ("on_hand", "in_transit", "on_order", "allocated", "backorder")
            )
        ):
            raise InvalidInput(
                "virtual bundle cannot also expose physical parent inventory; use a distinct prepacked SKU"
            )
    for inv in payload["global_supply_pool"]:
        v = select_bom(versions, inv["sku"], cp.captured_at.date())
        if (
            v
            and v["stock_mode"] == "virtual"
            and (inv["available_units"] or inv["reserved_units"])
        ):
            raise InvalidInput(
                "virtual bundle cannot also expose physical parent pool stock"
            )
