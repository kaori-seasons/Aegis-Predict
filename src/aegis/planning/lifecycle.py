"""Transactional plan lifecycle. Parent-row locks serialize adjustments, decisions and release."""
from __future__ import annotations

import gzip
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from ..core.audit import record_audit
from ..core.config import Settings, get_settings
from ..core.db import admin_session, tenant_session
from ..core.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from ..core.tenancy import Principal, current_trace_id, system_principal
from ..core.util import canonical_json, content_hash, json_safe, utcnow
from ..integration.outbox import emit
from ..models import (IntegrationConnection, InventorySnapshot, PlanInputBlob, PlanLine, PlanLinePin,
                      PlanRun, PolicyVersion, WritebackCommand)
from .assembler import assemble
from .engine import ALGORITHM_VERSION, solve
from .policy import PlanningPolicy
from .forecast import LEGACY_MODEL_VERSION, MODEL_VERSION, SUPPORTED_MODEL_VERSIONS

ACTIVE = ("queued", "solving", "pending_review", "released")


def _tenant_lock(session: Session, tenant_id: uuid.UUID) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": tenant_id.int % (2**63-1)})


def require_global_scope(principal: Principal) -> None:
    if principal.location_scope is not None:
        raise PermissionDenied("tenant-wide planning and integrations require unrestricted location scope")


def create_policy(session: Session, principal: Principal, config: dict, *, name: str = "default") -> PolicyVersion:
    require_global_scope(principal)
    parsed = PlanningPolicy.model_validate(config)
    _tenant_lock(session, principal.tenant_id)
    version = (session.scalar(select(func.max(PolicyVersion.version)).where(PolicyVersion.name == name)) or 0) + 1
    session.execute(update(PolicyVersion).where(PolicyVersion.is_active).values(is_active=False))
    row = PolicyVersion(id=uuid.uuid4(), tenant_id=principal.tenant_id, name=name, version=version,
                        config=parsed.model_dump(mode="json"), is_active=True, created_by=principal.label)
    session.add(row); session.flush()
    record_audit(session, principal, "policy.create", "policy_version", row.id, after={"version": version, "config": row.config})
    return row


def request_plan(session: Session, principal: Principal, *, horizon_days: int = 30, seed: int = 42,
                 cutoff_at=None, writeback_connection_id: uuid.UUID | None = None, mode: str = "shadow") -> PlanRun:
    require_global_scope(principal)
    _tenant_lock(session, principal.tenant_id)
    if session.scalar(select(PlanRun.id).where(PlanRun.status.in_(ACTIVE))):
        raise Conflict("close or discard the active plan before generating another; avoids double allocation")
    policy = session.scalar(select(PolicyVersion).where(PolicyVersion.is_active))
    if policy is None:
        raise InvalidInput("create an active planning policy first")
    cutoff = cutoff_at or utcnow()
    if mode not in ("shadow", "execution"):
        raise InvalidInput("mode must be shadow or execution")
    if mode == "execution" and not get_settings().plan_execution_enabled:
        raise InvalidInput("execution plans are disabled; use shadow mode")
    if writeback_connection_id and (mode != "execution" or not get_settings().writeback_enabled):
        raise InvalidInput("external writeback is disabled; use a shadow plan")
    if cutoff.tzinfo is None or cutoff > utcnow() + timedelta(seconds=5) or not 1 <= horizon_days <= 365:
        raise InvalidInput("invalid cutoff or horizon")
    if writeback_connection_id:
        connection = session.get(IntegrationConnection, writeback_connection_id)
        if connection is None or connection.status != "active":
            raise InvalidInput("writeback connection is unavailable")
    row = PlanRun(id=uuid.uuid4(), tenant_id=principal.tenant_id, status="queued", policy_version_id=policy.id,
                  horizon_days=horizon_days, cutoff_at=cutoff, seed=seed, requested_by=principal.label,
                  trace_id=current_trace_id(), writeback_connection_id=writeback_connection_id, mode=mode)
    if policy.config.get('algorithm_version') == 'aegis-protection-v3':
        from ..decision.history import checkpoint
        from ..models import KnowledgeCheckpoint
        if cutoff_at is None or abs((utcnow()-cutoff).total_seconds())<5:
            cp=checkpoint(session,principal.tenant_id)
        else:
            cp=session.scalar(select(KnowledgeCheckpoint).where(KnowledgeCheckpoint.captured_at<=cutoff).order_by(KnowledgeCheckpoint.captured_at.desc()).limit(1))
            if cp is None:raise InvalidInput('no recorded knowledge checkpoint at the historical cutoff; historical knowledge cannot be fabricated')
        row.cutoff_at=cp.captured_at
        row.data_watermarks={'decision_checkpoint_id':str(cp.id)}
    session.add(row); session.flush()
    record_audit(session, principal, "plan.generate", "plan_run", row.id, after={"policy": policy.id, "cutoff": cutoff})
    return row


def get_plan(session: Session, plan_id: uuid.UUID, *, lock: bool = False) -> PlanRun:
    row = session.scalar(select(PlanRun).where(PlanRun.id == plan_id).with_for_update() if lock else select(PlanRun).where(PlanRun.id == plan_id))
    if row is None:
        raise NotFound("plan not found")
    return row


def _editable(session: Session, principal: Principal, plan_id: uuid.UUID) -> PlanRun:
    require_global_scope(principal)
    row = get_plan(session, plan_id, lock=True)
    if row.status != "pending_review":
        raise Conflict("only pending_review plans can be changed")
    return row


def adjust_line(session: Session, principal: Principal, plan_id: uuid.UUID, line_id: uuid.UUID,
                units: int, note: str, *, pin: bool = True) -> PlanLine:
    plan = _editable(session, principal, plan_id)
    line = session.get(PlanLine, line_id)
    if line is None or line.plan_run_id != plan.id:
        raise NotFound("plan line not found")
    frozen = read_frozen(session, plan)
    pack = next(s["pack_size"] for s in frozen["network"]["skus"] if s["sku"] == line.sku_code)
    if not note.strip() or units < 0 or units > line.allocated_units or units % pack:
        raise InvalidInput("adjustment requires a reason and whole packs between 0 and allocated_units")
    before = {"units": line.final_units, "status": line.status, "pinned": line.pinned}
    remaining = units
    shipments = []
    original_shipments = line.detail.get("original_shipments", line.detail["shipments"])
    for ship in original_shipments:
        take = min(remaining, ship["units"])
        if take:
            shipments.append({**ship, "units": take})
            remaining -= take
    line.final_units = units
    line.unmet_units = max(0, line.recommended_units - units)
    line.detail = {**line.detail, "shipments": shipments, "original_shipments": original_shipments}
    line.transport_mode = "+".join(s["transport_mode"] for s in shipments)
    line.pinned = pin
    line.status = "proposed"
    line.decision_by = line.decision_at = None
    line.decision_note = note
    session.execute(update(PlanLinePin).where(PlanLinePin.sku_id == line.sku_id,
                    PlanLinePin.location_id == line.location_id, PlanLinePin.is_active).values(is_active=False))
    if pin:
        session.add(PlanLinePin(tenant_id=principal.tenant_id, sku_id=line.sku_id, location_id=line.location_id,
                               units=units, transport_mode=line.transport_mode if "+" not in line.transport_mode else None,
                               reason=note, created_by=principal.label, source_plan_line_id=line.id))
    record_audit(session, principal, "plan.adjust", "plan_line", line.id, before=before,
                 after={"units": units, "status": line.status, "pinned": pin, "note": note})
    return line


def decide_line(session: Session, principal: Principal, plan_id: uuid.UUID, line_id: uuid.UUID,
                *, approve: bool, note: str = "") -> PlanLine:
    plan = _editable(session, principal, plan_id)
    line = session.get(PlanLine, line_id)
    if line is None or line.plan_run_id != plan.id:
        raise NotFound("plan line not found")
    if approve and line.final_units <= 0:
        raise InvalidInput("cannot approve a zero-quantity line")
    before = {"status": line.status}
    line.status = "approved" if approve else "rejected"
    line.decision_by, line.decision_at, line.decision_note = principal.label, utcnow(), note
    record_audit(session, principal, "plan.approve" if approve else "plan.reject", "plan_line", line.id,
                 before=before, after={"status": line.status, "units": line.final_units, "note": note})
    return line


def release_plan(session: Session, principal: Principal, plan_id: uuid.UUID, *, acknowledge_stale: bool = False,
                 command_type: str = "create_transfer", note: str = "") -> PlanRun:
    plan = get_plan(session, plan_id, lock=True)
    require_global_scope(principal)
    if plan.status == "released":
        return plan  # Idempotent client retries.
    if plan.status != "pending_review":
        raise Conflict("plan must be pending_review")
    if plan.mode != "execution":
        raise Conflict("shadow plans cannot reserve inventory or be released")
    if not get_settings().plan_execution_enabled:
        raise Conflict("execution plans are disabled")
    if plan.writeback_connection_id and not get_settings().writeback_enabled:
        raise Conflict("external writeback is disabled")
    lines = list(session.scalars(select(PlanLine).where(PlanLine.plan_run_id == plan.id).order_by(PlanLine.id)))
    if any(l.status == "proposed" and l.final_units > 0 for l in lines):
        raise Conflict("decide all nonzero lines before release")
    approved = [l for l in lines if l.status == "approved" and l.final_units > 0]
    if not approved:
        raise Conflict("no approved shipments")
    frozen = read_frozen(session, plan)
    policy = PlanningPolicy.model_validate(frozen["policy"])
    if frozen.get('decision'):
        from ..decision.protection import check_release
        check_release(session,policy,frozen['decision'])
    expired = (utcnow() - plan.cutoff_at).total_seconds() / 3600 > policy.max_inventory_age_hours
    if (plan.stale_data or expired) and not acknowledge_stale:
        raise Conflict("stale data must be explicitly acknowledged by the releasing approver")
    if acknowledge_stale:
        plan.stale_acknowledged_by = principal.label
    # Approval of an older snapshot cannot bypass a new failed/partial critical stream.
    if frozen.get('decision'):
        from ..decision.history import checkpoint,freeze_input
        from .assembler import AssembledNetwork
        evidence=freeze_input(session,principal,checkpoint(session,principal.tenant_id),policy)
        d=evidence.payload
        current=AssembledNetwork(d['network'],d['ids'],d['pins'],d['watermarks'],d['freshness'],d['stale'])
        from ..decision.protection import validate_current_capacity
        validate_current_capacity(policy,frozen['decision'],d,approved)
    else:
        current = assemble(session, principal.tenant_id, utcnow(), policy)
    if current.watermarks["authorities"] != plan.data_watermarks.get("authorities"):
        raise Conflict("source authority changed after planning; generate and review a new plan")
    if current.stale and not acknowledge_stale:
        raise Conflict("current authoritative data is stale and requires acknowledgement")
    from ..integration.reconciliation import require_reconciled
    settings = get_settings()
    if settings.require_reconciliation:
        require_reconciled(session, principal.tenant_id, live=settings.integration_mode == "live")
    source_connections = [session.get(IntegrationConnection, uuid.UUID(r["connection_id"])) for r in current.watermarks["authorities"]]
    source_connections.extend(session.get(IntegrationConnection, uuid.UUID(r["connection_id"])) for r in current.watermarks.get("external_signal_sources", []))
    if current.watermarks.get('market_source'):
        source_connections.append(session.get(IntegrationConnection, uuid.UUID(current.watermarks['market_source']['connection_id'])))
    if plan.writeback_connection_id:
        source_connections.append(session.get(IntegrationConnection, plan.writeback_connection_id))
    if any(c.is_mock != (settings.integration_mode == "mock") for c in source_connections):
        raise Conflict("execution cannot mix mock and live systems")
    from ..inventory.service import reserve_line
    for line in sorted(approved, key=lambda row: (str(row.sku_id), str(row.id))):
        reserve_line(session, principal, line, uuid.UUID(frozen["ids"]["pool"]))
    if plan.writeback_connection_id:
        from ..integration.registry import get_connector
        from ..integration.spi import WritebackConnector
        connection = session.get(IntegrationConnection, plan.writeback_connection_id)
        connector = get_connector(connection.kind) if connection else None
        if connection is None or connection.status != "active" or not isinstance(connector, WritebackConnector):
            raise InvalidInput("connection does not support writeback")
        if command_type not in connector.writeback_commands(connection.config):
            raise InvalidInput("writeback command type is unsupported")
        for line in approved:
            for shipment in line.detail["shipments"]:
                key = content_hash([str(principal.tenant_id), str(plan.id), str(line.id), command_type, shipment["transport_mode"]])
                command = WritebackCommand(tenant_id=principal.tenant_id, connection_id=connection.id,
                    plan_run_id=plan.id, plan_line_id=line.id, command_type=command_type, idempotency_key=key,
                    payload={**shipment, "plan_run_id": str(plan.id), "plan_line_id": str(line.id)}, status="pending")
                session.add(command)
                emit(session, tenant_id=principal.tenant_id, topic="writeback.requested", aggregate_type="plan_line",
                     aggregate_id=line.id, payload={"idempotency_key": key})
    for line in approved:
        line.status = "released"
    plan.status, plan.released_at, plan.released_by = "released", utcnow(), principal.label
    record_audit(session, principal, "plan.release", "plan_run", plan.id,
                 after={"status": plan.status, "approved_lines": len(approved), "stale_acknowledged": acknowledge_stale, "note": note})
    return plan


def terminate_plan(session: Session, principal: Principal, plan_id: uuid.UUID, *, action: str, note: str) -> PlanRun:
    require_global_scope(principal)
    plan = get_plan(session, plan_id, lock=True)
    if not note.strip():
        raise InvalidInput("reason required")
    if action == "cancel" and plan.status in ("queued", "solving"):
        plan.cancel_requested = True
        if plan.status == "queued":
            plan.status = "cancelled"
    elif action == "discard" and plan.status == "pending_review":
        plan.status = "discarded"
    elif action == "close" and plan.status == "released":
        from ..inventory.execution import assert_plan_complete
        from ..integration.reconciliation import require_reconciled
        assert_plan_complete(session, plan)
        if get_settings().require_reconciliation:
            require_reconciled(session, principal.tenant_id, live=get_settings().integration_mode == "live")
        current = assemble(session, principal.tenant_id, utcnow(), PlanningPolicy.model_validate(read_frozen(session, plan)["policy"]))
        if current.stale:
            raise Conflict("current inventory must be fresh before closing")
        plan.status, plan.closed_at = "closed", utcnow()
    else:
        raise Conflict(f"cannot {action} plan in {plan.status}")
    record_audit(session, principal, f"plan.{action}", "plan_run", plan.id, after={"status": plan.status, "note": note})
    session.flush()
    return plan


def read_frozen(session: Session, plan: PlanRun) -> dict:
    blob = session.get(PlanInputBlob, plan.id)
    if blob is None:
        raise Conflict("plan input has not been frozen yet")
    document = json.loads(gzip.decompress(blob.content))
    if content_hash(document) != blob.digest or blob.digest != plan.input_digest:
        raise Conflict("frozen input digest mismatch")
    return document


def reproduce(session: Session, plan: PlanRun) -> dict:
    frozen = read_frozen(session, plan)
    if frozen.get("model_version",LEGACY_MODEL_VERSION) not in SUPPORTED_MODEL_VERSIONS:
        raise Conflict("reproduction requires the original forecast model image")
    if frozen["algorithm_version"] not in (ALGORITHM_VERSION, "aegis-protection-v3"):
        raise Conflict("reproduction requires the original algorithm image")
    return solve(frozen["network"], PlanningPolicy.model_validate(frozen["policy"]), frozen["horizon_days"],
                 seed=frozen["seed"], pins=frozen["pins"],
                       model_version=frozen.get("model_version", LEGACY_MODEL_VERSION), decision=frozen.get("decision"))


@dataclass(frozen=True)
class ClaimedPlan:
    id: uuid.UUID
    tenant_id: uuid.UUID
    owner: str


def claim_plan(settings: Settings | None = None) -> ClaimedPlan | None:
    settings = settings or get_settings()
    with admin_session() as session:
        plan = session.scalar(select(PlanRun).where(PlanRun.status == "queued").order_by(PlanRun.created_at)
                              .limit(1).with_for_update(skip_locked=True))
        if plan is None:
            return None
        plan.status, plan.started_at = "solving", utcnow()
        plan.attempts += 1
        plan.lease_owner = f"{settings.worker_id}:{uuid.uuid4().hex}"
        plan.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
        return ClaimedPlan(plan.id, plan.tenant_id, plan.lease_owner)


def _fence(session: Session, claim: ClaimedPlan) -> PlanRun:
    plan = get_plan(session, claim.id, lock=True)
    if plan.status != "solving" or plan.lease_owner != claim.owner or plan.lease_expires_at <= utcnow():
        raise Conflict("plan worker lease lost", code="lease_lost")
    return plan


def execute_plan(claim: ClaimedPlan) -> str:
    try:
        with tenant_session(claim.tenant_id, isolation_level="REPEATABLE READ") as session:
            plan = _fence(session, claim)
            if plan.cancel_requested:
                plan.status = "cancelled"
                return plan.status
            if not plan.input_digest:
                policy = session.get(PolicyVersion, plan.policy_version_id)
                config = PlanningPolicy.model_validate(policy.config)
                if config.algorithm_version == 'aegis-protection-v3':
                    from ..decision.protection import freeze_decision
                    from ..decision.history import require_checkpoint
                    from .assembler import AssembledNetwork
                    cp=require_checkpoint(session,uuid.UUID(plan.data_watermarks['decision_checkpoint_id']))
                    evidence,decision=freeze_decision(session,system_principal(claim.tenant_id),config,cp,execution=plan.mode=='execution')
                    d=evidence.payload
                    assembled=AssembledNetwork(d['network'],d['ids'],d['pins'],d['watermarks'],d['freshness'],d['stale'])
                else:
                    decision=None
                    assembled = assemble(session, claim.tenant_id, plan.cutoff_at, config)
                frozen = {"network": assembled.payload, "ids": assembled.ids, "pins": assembled.pins,
                          "policy": policy.config, "horizon_days": plan.horizon_days, "seed": plan.seed,
                          "algorithm_version": config.algorithm_version, "model_version": config.forecast_model_version}
                if decision is not None:frozen['decision']=decision
                digest = content_hash(frozen)
                session.add(PlanInputBlob(plan_run_id=plan.id, tenant_id=claim.tenant_id, digest=digest,
                                          content=gzip.compress(canonical_json(frozen).encode(), mtime=0)))
                plan.input_digest, plan.data_watermarks = digest, assembled.watermarks
                plan.stale_data, plan.freshness = assembled.stale, json_safe(assembled.freshness)
            else:
                frozen = read_frozen(session, plan)
        result = solve(frozen["network"], PlanningPolicy.model_validate(frozen["policy"]), frozen["horizon_days"],
                       seed=frozen["seed"], pins=frozen["pins"],
                       model_version=frozen.get("model_version", LEGACY_MODEL_VERSION), decision=frozen.get("decision"))
        with tenant_session(claim.tenant_id) as session:
            plan = _fence(session, claim)
            if plan.cancel_requested:
                plan.status = "cancelled"
                return plan.status
            for row in result["lines"]:
                session.add(PlanLine(tenant_id=claim.tenant_id, plan_run_id=plan.id,
                    sku_id=uuid.UUID(frozen["ids"]["skus"][row["sku"]]), location_id=uuid.UUID(frozen["ids"]["locations"][row["warehouse_id"]]),
                    sku_code=row["sku"], location_code=row["warehouse_id"], daily_rate=row["daily_rate"],
                    inventory_position=row["inventory_position"], reorder_point=row["reorder_point"], target_stock=row["target_stock"],
                    recommended_units=row["recommended_units"], allocated_units=row["allocated_units"], unmet_units=row["unmet_units"],
                    final_units=row["final_units"], transport_mode=row["transport_mode"], lead_time_days=row["lead_time_days"],
                    urgency=row["urgency"], priority_score=row["urgency"], approval_required=row["approval_required"],
                    pinned=row["pinned"], status="proposed", reason=row["reason"], detail=row))
            plan.status, plan.finished_at = "pending_review", utcnow()
            plan.model_name, plan.algorithm_version = result["model_version"], result["algorithm_version"]
            plan.summary, plan.optimizer_status = result["summary"], result["optimizer"]["status"]
            plan.lease_owner = plan.lease_expires_at = None
            emit(session, tenant_id=claim.tenant_id, topic="plan.ready", aggregate_type="plan_run", aggregate_id=plan.id,
                 payload={"summary": result["summary"], "input_digest": plan.input_digest})
        return "pending_review"
    except Exception as exc:
        with tenant_session(claim.tenant_id) as session:
            plan = get_plan(session, claim.id, lock=True)
            if plan.status == "solving" and plan.lease_owner == claim.owner and plan.lease_expires_at > utcnow():
                plan.status, plan.error, plan.finished_at = "failed", str(exc)[:4000], utcnow()
                plan.lease_owner = plan.lease_expires_at = None
            return plan.status


def sweep_plan_leases(session: Session, settings: Settings, now) -> int:
    plans = session.scalars(select(PlanRun).where(PlanRun.status == "solving", PlanRun.lease_expires_at < now)
                            .with_for_update(skip_locked=True)).all()
    for p in plans:
        p.status = "cancelled" if p.cancel_requested else "failed" if p.attempts >= settings.plan_run_max_attempts else "queued"
        p.error = "worker lease expired"
        p.lease_owner = p.lease_expires_at = None
    return len(plans)


def run_plan_once() -> str | None:
    claim = claim_plan()
    return execute_plan(claim) if claim else None
