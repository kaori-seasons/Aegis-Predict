"""Freeze selected independent contracts; never query live tables during a replay."""

from sqlalchemy import select

from ..core.errors import InvalidInput
from ..core.util import json_safe
from ..integration.commerce import TABLES
from ..models import IntegrationConnection


def freeze(session, tenant, cutoff, policy, skus, locations):
    config = {
        k: getattr(policy, k)
        for k in (
            "commerce_signal_source",
            "commerce_signal_channel",
            "commerce_contract_version",
            "launch_signal_source",
            "launch_signal_channel",
            "public_signal_source",
        )
    }
    selections = {
        "commerce_performance": config["commerce_signal_source"],
        "sku_launches": config["launch_signal_source"],
        "public_signals": config["public_signal_source"],
        "public_signal_bindings": config["public_signal_source"],
    }
    result = {"config": config}
    sources = []
    for name, source in selections.items():
        result[name] = []
        if not source:
            continue
        c = session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.source_system == source,
                IntegrationConnection.status == "active",
            )
        )
        if c is None:
            raise InvalidInput(f"{name}: selected active source is unavailable")
        if not any(r["connection_id"] == str(c.id) for r in sources):
            sources.append(
                {
                    "connection_id": str(c.id),
                    "source_system": source,
                    "is_mock": c.is_mock,
                }
            )
        model = TABLES[name]
        query = select(model).where(
            model.tenant_id == tenant,
            model.source_system == source,
            model.published_at <= cutoff,
            model.received_at <= cutoff,
            model.available_at <= cutoff,
        )
        if name == "commerce_performance":
            query = query.where(
                model.channel == config["commerce_signal_channel"],
                model.contract_version == config["commerce_contract_version"],
            )
        if name == "sku_launches":
            query = query.where(model.channel == config["launch_signal_channel"])
        # Include tombstones and all versions; each inner origin selects its own latest.
        for r in session.scalars(
            query.order_by(model.available_at, model.id).limit(50001)
        ):
            if len(result[name]) == 50000:
                raise InvalidInput("commerce freeze budget exceeded")
            row = {
                col.name: getattr(r, col.name)
                for col in model.__table__.columns
                if col.name not in ("tenant_id", "content_hash")
            }
            if hasattr(r, "sku_id"):
                if r.sku_id not in skus:
                    continue
                row["sku"] = skus[r.sku_id].code
            if hasattr(r, "location_id"):
                if r.location_id not in locations:
                    continue
                row["warehouse_id"] = locations[r.location_id].code
            result[name].append(row)
    return json_safe(result), sources
