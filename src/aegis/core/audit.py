from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog
from .crypto import redact
from .tenancy import Principal, current_trace_id
from .util import json_safe


def _diff(before: dict | None, after: dict | None) -> tuple[dict | None, dict | None]:
    """Keep only changed keys so audit rows stay small and readable."""

    if before is None or after is None:
        return before, after
    keys = sorted(set(before) | set(after))
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return {key: before.get(key) for key in changed}, {key: after.get(key) for key in changed}


def record_audit(
    session: Session,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: Any = None,
    *,
    before: dict | None = None,
    after: dict | None = None,
    outcome: str = "success",
    ip: str | None = None,
) -> None:
    """Append an audit row in the caller's transaction (the action and its audit commit atomically)."""

    before_clean = json_safe(redact(before)) if before is not None else None
    after_clean = json_safe(redact(after)) if after is not None else None
    before_clean, after_clean = _diff(before_clean, after_clean)
    session.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            actor_label=principal.label,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            outcome=outcome,
            before=before_clean,
            after=after_clean,
            trace_id=current_trace_id(),
            ip=ip,
        )
    )
