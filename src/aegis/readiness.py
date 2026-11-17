"""Read-only evidence report: data readiness is deliberately separate from live-pilot approval."""
from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from .core.errors import AegisError
from .core.util import utcnow
from .models import IntegrationConnection, SourceAuthority
from .planning.assembler import assemble
from .planning.policy import PlanningPolicy


def pilot_report(session: Session, tenant_id) -> dict:
    connections = list(session.scalars(select(IntegrationConnection)))
    rules = list(session.scalars(select(SourceAuthority)))
    blockers = []
    summary = None
    try:
        # Read-only current visibility preview. Unlike a forecast issuance this
        # does not persist a checkpoint/artifact or pretend to be a past origin.
        import uuid
        from sqlalchemy import text
        from .models import KnowledgeCheckpoint
        from .decision.history import replay
        from .decision.bundles import validate_stock_shape
        clock,snapshot=session.execute(text('SELECT clock_timestamp(),pg_current_snapshot()::text')).one()
        cp=KnowledgeCheckpoint(id=uuid.uuid4(),tenant_id=tenant_id,captured_at=clock,pg_snapshot=snapshot)
        with replay(session,cp):
            assembled = assemble(session, tenant_id, clock, PlanningPolicy())
            validate_stock_shape(session,cp,assembled.payload)
        summary = {"demand_series": len(assembled.payload["demand_history"]),
                   "inventory_snapshots": len(assembled.watermarks["inventory"]),
                   "issues": assembled.freshness["issues"]}
        if assembled.stale:
            blockers.extend(assembled.freshness["issues"])
    except AegisError as exc:
        blockers.append({"code": exc.code, "message": exc.message, "details": exc.details})
    from .core.config import get_settings
    from .integration.reconciliation import require_reconciled
    settings = get_settings()
    checks = []
    if settings.require_reconciliation:
        try:
            checks = require_reconciled(session, tenant_id, live=settings.integration_mode == "live")
        except AegisError as exc:
            blockers.append({"code": exc.code, "message": exc.message, "details": exc.details})
    platform_connections = [c for c in connections if c.kind in ("amazon_sp", "tiktok_shop", "mock_amazon", "mock_tiktok")]
    return {"tenant_id": str(tenant_id), "data_ready": not blockers, "data_blockers": blockers,
            "summary": summary, "authority_count": len(rules),
            "platform_connections": [{"id": str(c.id), "kind": c.kind, "status": c.status,
                                      "credentials_stored": bool(c.secret_ciphertext), "is_mock": c.is_mock} for c in platform_connections],
            "integration_mode": settings.integration_mode, "reconciliation_ids": checks,
            "live_pilot_approved": False,
            "external_evidence_required": ["real shop authorization and market contract tests",
                "signed source/quantity/timezone contract", "14-day source reconciliation and operator UAT"],
            "execution_limit": "mock/live sources must be isolated; live approval requires external UAT and authority contracts"}
