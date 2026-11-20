"""Frozen cumulative forecasts -> route protection targets and time-phased allocation."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from math import ceil
from zoneinfo import ZoneInfo

from ..core.errors import Conflict, InvalidInput
from ..core.util import utcnow
from ..models import DecisionCase
from .history import facts, freeze_input, verified

ALGORITHM_VERSION = "aegis-protection-v3"


def freeze_decision(session, p, policy, cp, *, execution=False):
    from .forecasting import scope

    fc = verified(session, uuid.UUID(policy.forecast_result_id), "forecast")
    inp = freeze_input(session, p, cp, policy)
    if fc.payload["scope"] != scope(inp.payload, fc.payload["horizons"]):
        raise InvalidInput("forecast and current policy feature/horizon scope differ")
    if (
        cp.captured_at - datetime.fromisoformat(fc.payload["as_of"])
    ).total_seconds() > 86400 or datetime.fromisoformat(
        fc.payload["as_of"]
    ) > cp.captured_at:
        raise InvalidInput(
            "forecast is stale or was issued after the planning checkpoint"
        )
    old = verified(session, uuid.UUID(fc.payload["input_id"]), "input")
    if (
        old.payload["watermarks"]["authorities"]
        != inp.payload["watermarks"]["authorities"]
    ):
        raise Conflict("forecast source ownership changed; regenerate forecasts")
    if old.payload["fact_revisions"].get("bom_versions", []) != inp.payload[
        "fact_revisions"
    ].get("bom_versions", []):
        raise Conflict(
            "BOM knowledge changed after forecast issuance; regenerate forecasts"
        )
    chosen = fc
    if policy.forecast_scenario_id:
        chosen = verified(session, uuid.UUID(policy.forecast_scenario_id), "scenario")
        if chosen.payload["baseline_id"] != str(fc.id):
            raise InvalidInput("scenario baseline mismatch")
        for ov in chosen.payload["overrides"]:
            row = session.get(DecisionCase, uuid.UUID(ov["id"]))
            if (
                row is None
                or row.status != "approved"
                or row.revision != ov["revision"]
                or datetime.fromisoformat(ov["expires_at"]) <= cp.captured_at
            ):
                raise Conflict("scenario assumptions expired or were revoked")
    if execution:
        require_release(session, policy, fc)
        if inp.payload["stale"]:
            raise InvalidInput("new protection execution requires fresh inputs")
        if any(v["unknown_inbound_units"] for v in inp.payload["arrivals"].values()):
            raise InvalidInput(
                "unknown inbound ETA/attribution blocks time-phased execution"
            )
    return inp, {
        "forecast_id": str(fc.id),
        "forecast_digest": fc.digest,
        "scenario_id": str(chosen.id) if chosen.kind == "scenario" else None,
        "prediction": chosen.payload,
        "arrivals": inp.payload["arrivals"],
        "checkpoint_id": str(cp.id),
        "bom_fact_revisions": inp.payload["fact_revisions"].get("bom_versions", []),
        "execution_eligible": execution,
    }


def require_release(session, policy, fc):
    from .jobs import code_digest

    for forecast_row in fc.payload["rows"]:
        if forecast_row["model"] == policy.forecast_model_version and forecast_row.get("evidence",{}).get("execution_blockers"):
            raise Conflict("forecast has unresolved cold-start validation blockers")

    if fc.payload["code_digest"] != code_digest():
        raise Conflict("execution requires the validated forecast code image")
    row = (
        session.get(DecisionCase, uuid.UUID(policy.model_release_id))
        if policy.model_release_id
        else None
    )
    if (
        row is None
        or row.kind != "model_release"
        or row.status != "active"
        or row.payload["scope"] != fc.payload["scope"]
        or row.payload["model"] != policy.forecast_model_version
        or row.payload["calibration_id"] != fc.payload["calibration_id"]
    ):
        raise Conflict(
            "execution requires an active, matching, independently validated model release"
        )
    for r in fc.payload["rows"]:
        if r["model"] == policy.forecast_model_version and not all(
            v["calibrated"] for v in r["horizons"].values()
        ):
            raise Conflict(
                "execution forecast is not calibrated for every protection horizon"
            )


def check_release(session, policy, decision):
    fc = verified(session, uuid.UUID(decision["forecast_id"]), "forecast")
    require_release(session, policy, fc)
    if (utcnow() - datetime.fromisoformat(fc.payload["as_of"])).total_seconds() > 86400:
        raise Conflict("forecast expired before execution")
    from .history import checkpoint

    cp = checkpoint(session, fc.tenant_id)
    current = [i for _, i in facts(session, cp, "decision_artifact")]
    if current != decision["bom_fact_revisions"]:
        raise Conflict("BOM changed after planning; regenerate and review the plan")
    if decision.get("scenario_id"):
        sc = verified(session, uuid.UUID(decision["scenario_id"]), "scenario")
        for ov in sc.payload["overrides"]:
            row = session.get(DecisionCase, uuid.UUID(ov["id"]))
            if (
                row is None
                or row.status != "approved"
                or row.revision != ov["revision"]
                or datetime.fromisoformat(ov["expires_at"]) <= utcnow()
            ):
                raise Conflict("operational assumption is no longer approved")


def solve_protection(payload, policy, decision, *, seed=42, pins=None):
    from overseas_inventory.models import network_from_payload

    from ..planning.engine import _optimize, _options, _penalty, validate_network

    if not decision:
        raise ValueError("protection-v3 requires frozen decision evidence")
    network = network_from_payload(payload)
    validate_network(network)
    prediction = decision["prediction"]
    model = policy.forecast_model_version
    forecasts = {
        (r["sku"], r["warehouse_id"]): r
        for r in prediction["rows"]
        if r["model"] == model
    }
    lines = []
    q = {0.5: "p50", 0.8: "p80", 0.95: "p95"}[policy.protection_quantile]
    for series in sorted(network.demand_history, key=lambda s: (s.sku, s.warehouse_id)):
        key = (series.sku, series.warehouse_id)
        fc = forecasts.get(key)
        if fc is None:
            raise ValueError("missing series in immutable forecast")
        start = date.fromisoformat(next(iter(fc["horizons"].values()))["target_start"])
        as_of = datetime.fromisoformat(payload["forecast_as_of"])
        if as_of.astimezone(ZoneInfo(fc["timezone"])).date() != start:
            raise ValueError(
                "forecast and plan must share a local target-start day; regenerate rather than extrapolate"
            )
        options = _options(network, key[1])
        lane = network.lane_from_pool(key[1])
        sku = network.skus[key[0]]
        inv = network.inventory[key]
        needed = {
            d
            for o in options
            for d in (
                max(1, o.lead_time_days),
                o.lead_time_days + policy.review_period_days,
            )
        }
        if any(str(h) not in fc["horizons"] for h in needed):
            raise ValueError(
                "forecast artifact lacks a route lead/protection horizon; generate all required horizons"
            )
        arrival = decision["arrivals"].get(
            "/".join(key), {"arrivals": [], "unknown_inbound_units": 0}
        )
        now_stock = inv.on_hand - inv.allocated - inv.backorder

        def due(h):
            return sum(a["units"] for a in arrival["arrivals"] if a["day"] <= h)

        routes = []
        for option in options:
            lead = max(1, option.lead_time_days)
            h = option.lead_time_days + policy.review_period_days
            buffer = ceil(
                sum(fc["daily"][:lead])
                / lead
                * policy.service_level_z.get(sku.service_class, 1.28)
                * lane.lead_time_std_days
            )
            target = ceil(fc["horizons"][str(h)][q]) + buffer
            reorder = ceil(fc["horizons"][str(lead)][q]) + buffer
            need = max(
                0, ceil((target - now_stock - due(h)) / sku.pack_size) * sku.pack_size
            )
            timeline = []
            cumulative = 0.0
            for day, value in enumerate(fc["daily"][:h], 1):
                cumulative += value
                req = max(
                    0, ceil((target if day == h else cumulative) - now_stock - due(day))
                )
                timeline.append({"day": day, "units": req})
            bridge = max(
                (r["units"] for r in timeline if r["day"] > option.lead_time_days),
                default=0,
            )
            need = max(need, ceil(bridge / sku.pack_size) * sku.pack_size)
            routes.append(
                {
                    "mode": option.mode,
                    "lead": option.lead_time_days,
                    "protection": h,
                    "target": target,
                    "reorder": reorder,
                    "need": need,
                    "lead_buffer": buffer,
                    "timeline": timeline,
                }
            )
        base = max(routes, key=lambda r: r["protection"])
        protection = base["protection"]
        lead = max(1, base["lead"])
        target = base["target"]
        reorder = base["reorder"]
        need = max(r["need"] for r in routes)
        lead_buffer = base["lead_buffer"]
        time_requirements = base["timeline"]
        purchases = []
        cumulative_intent = 0
        for intent in sorted(
            prediction.get("purchase_intents", []), key=lambda r: r["end"]
        ):
            if (intent["sku"], intent["warehouse_id"]) == key and intent.get(
                "model", model
            ) == model:
                day = (date.fromisoformat(intent["end"]) - start).days + 1
                if day > max(prediction["horizons"]):
                    raise ValueError(
                        "purchase intent deadline exceeds the forecast planning horizon"
                    )
                cumulative_intent += intent["units"]
                purchases.append({"day": day, "units": cumulative_intent})
                need = max(need, cumulative_intent)
                for route in routes:
                    route["need"] = max(route["need"], cumulative_intent)
        urgency = min(
            1.0, max(0.0, (reorder - now_stock - due(lead)) / max(1, reorder))
        )
        lines.append(
            {
                "sku": key[0],
                "warehouse_id": key[1],
                "forecast": fc,
                "daily_rate": sum(fc["daily"][:protection]) / protection,
                "inventory_position": now_stock + due(protection),
                "safety_stock": max(0, target - ceil(sum(fc["daily"][:protection]))),
                "reorder_point": reorder,
                "target_stock": target,
                "recommended_units": need,
                "urgency": urgency,
                "pinned": False,
                "approval_required": True,
                "reason": "按冻结每日轨迹、真实保护期与可证实到货日补货",
                "protection_days": protection,
                "quantile": q,
                "lead_time_buffer_units": lead_buffer,
                "uncertainty_calibrated": all(
                    v["calibrated"] for v in fc["horizons"].values()
                ),
                "unknown_inbound_units": arrival["unknown_inbound_units"],
                "time_requirements": time_requirements,
                "route_requirements": routes,
                "purchase_requirements": purchases,
            }
        )
    pinmap = {}
    bykey = {(r["sku"], r["warehouse_id"]): r for r in lines}
    for pin in pins or []:
        key = (pin["sku"], pin["warehouse_id"])
        if (
            key not in bykey
            or key in pinmap
            or pin["units"] < 0
            or pin["units"] % network.skus[key[0]].pack_size
        ):
            raise ValueError("invalid pinned quantity")
        bykey[key]["recommended_units"] = max(
            bykey[key]["recommended_units"], pin["units"]
        )
        bykey[key]["pinned"] = True
        pinmap[key] = pin
        for route in bykey[key]["route_requirements"]:
            route["need"] = max(route["need"], pin["units"])
    shipments, diagnostics = _optimize(network, lines, policy, pinmap)
    temporal_shortage = 0
    for row in lines:
        mode = diagnostics.get("selected_modes", {}).get(
            row["warehouse_id"], row["route_requirements"][0]["mode"]
        )
        route = next(r for r in row["route_requirements"] if r["mode"] == mode)
        row.update(
            recommended_units=route["need"],
            target_stock=route["target"],
            reorder_point=route["reorder"],
            protection_days=route["protection"],
            time_requirements=route["timeline"],
            lead_time_buffer_units=route["lead_buffer"],
            selected_mode=mode,
        )
        row["daily_rate"] = (
            sum(row["forecast"]["daily"][: route["protection"]]) / route["protection"]
        )
        row["inventory_position"] = (
            network.inventory[(row["sku"], row["warehouse_id"])].on_hand
            - network.inventory[(row["sku"], row["warehouse_id"])].allocated
            - network.inventory[(row["sku"], row["warehouse_id"])].backorder
            + sum(
                a["units"]
                for a in decision["arrivals"]
                .get(row["sku"] + "/" + row["warehouse_id"], {})
                .get("arrivals", [])
                if a["day"] <= route["protection"]
            )
        )
        row["safety_stock"] = max(
            0,
            route["target"]
            - ceil(sum(row["forecast"]["daily"][: route["protection"]])),
        )
        row["shipments"] = [
            s
            for s in shipments
            if (s["sku"], s["warehouse_id"]) == (row["sku"], row["warehouse_id"])
        ]
        row["allocated_units"] = row["final_units"] = sum(
            s["units"] for s in row["shipments"]
        )
        row["unmet_units"] = row["recommended_units"] - row["allocated_units"]
        row["transport_mode"] = "+".join(s["transport_mode"] for s in row["shipments"])
        row["lead_time_days"] = max(
            (s["lead_time_days"] for s in row["shipments"]), default=0
        )
        for req in row["time_requirements"]:
            req["unmet_units"] = max(
                0,
                req["units"]
                - sum(
                    s["units"]
                    for s in row["shipments"]
                    if s["lead_time_days"] < req["day"]
                ),
            )
        row["unavoidable_prearrival_units"] = max(
            (
                r["unmet_units"]
                for r in row["time_requirements"]
                if r["day"] <= route["lead"]
            ),
            default=0,
        )
        temporal_shortage += sum(r["unmet_units"] for r in row["time_requirements"])
    dispatch = sum(s["units"] * s["unit_total_cost"] for s in shipments)
    penalty = sum(
        sum(t["unmet_units"] for t in r["time_requirements"]) * _penalty(r, policy)
        for r in lines
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "model_version": model,
        "seed": seed,
        "lines": lines,
        "optimizer": diagnostics,
        "summary": {
            "recommended_units": sum(r["recommended_units"] for r in lines),
            "allocated_units": sum(r["allocated_units"] for r in lines),
            "unmet_units": sum(r["unmet_units"] for r in lines),
            "dispatch_cost": round(dispatch, 4),
            "shortage_penalty": round(penalty, 4),
            "total_cost": round(dispatch + penalty, 4),
            "projected_shortage_unit_days": temporal_shortage,
            "forecast_id": decision["forecast_id"],
            "execution_eligible": decision["execution_eligible"],
        },
    }


def validate_current_capacity(policy, decision, current, approved):
    """Execution cannot acknowledge away a changed temporal/capacity assumption."""
    from collections import defaultdict

    from overseas_inventory.models import network_from_payload

    from ..planning.engine import _capacity, _options

    if current["stale"]:
        raise Conflict("fresh point-in-time data is required for protection execution")
    if current["arrivals"] != decision["arrivals"]:
        raise Conflict(
            "inbound quantities or ETA changed; replan the time-phased allocation"
        )
    if policy.forecast_model_version == "commerce-coldstart-v5":
        from .forecasting import scope
        if scope(current,decision["prediction"]["horizons"]) != decision["prediction"]["scope"]:
            raise Conflict("commerce/launch/public mapping contract changed; regenerate and review the forecast")
    network = network_from_payload(current["network"])
    bywh = defaultdict(int)
    bysku = defaultdict(int)
    byroute = defaultdict(int)
    for line in approved:
        fc = next(
            r
            for r in decision["prediction"]["rows"]
            if (r["sku"], r["warehouse_id"], r["model"])
            == (line.sku_code, line.location_code, policy.forecast_model_version)
        )
        first = next(iter(fc["horizons"].values()))["target_start"]
        if (
            datetime.fromisoformat(current["as_of"])
            .astimezone(ZoneInfo(fc["timezone"]))
            .date()
            .isoformat()
            != first
        ):
            raise Conflict("forecast local day changed; generate a fresh plan")
        bywh[line.location_code] += line.final_units
        bysku[line.sku_code] += line.final_units
        for shipment in line.detail["shipments"]:
            byroute[(line.location_code, shipment["transport_mode"])] += shipment[
                "units"
            ]
    cumulative_intent = defaultdict(int)
    for intent in sorted(
        decision["prediction"].get("purchase_intents", []), key=lambda r: r["end"]
    ):
        if (
            intent.get("model", policy.forecast_model_version)
            != policy.forecast_model_version
        ):
            continue
        key = (intent["sku"], intent["warehouse_id"])
        cumulative_intent[key] += intent["units"]
        fc = next(
            r
            for r in decision["prediction"]["rows"]
            if (r["sku"], r["warehouse_id"], r["model"])
            == (*key, policy.forecast_model_version)
        )
        start = date.fromisoformat(next(iter(fc["horizons"].values()))["target_start"])
        deadline = (date.fromisoformat(intent["end"]) - start).days + 1
        delivered = sum(
            ship["units"]
            for line in approved
            if (line.sku_code, line.location_code) == key
            for ship in line.detail["shipments"]
            if ship["lead_time_days"] <= deadline
        )
        if delivered < cumulative_intent[key]:
            raise Conflict(
                "approved purchase intent cannot be bypassed by rejecting or reducing a plan line; revise and reapprove the assumption"
            )
    for wh, units in bywh.items():
        if units > _capacity(network, wh):
            raise Conflict("warehouse capacity changed after approval")
    for sku, units in bysku.items():
        terms = network.procurement_constraints.get(sku)
        if terms and (
            units > terms.available_units
            or terms.unit_purchase_cost > 0
            and units * terms.unit_purchase_cost > terms.capital_budget
            or units < terms.min_order_qty
        ):
            raise Conflict("supplier quota, budget or MOQ changed after approval")
    for (wh, mode), units in byroute.items():
        option = next((o for o in _options(network, wh) if o.mode == mode), None)
        if option is None or units > option.max_units:
            raise Conflict("transport route capacity changed after approval")
