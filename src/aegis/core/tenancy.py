from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

_current_tenant: ContextVar[uuid.UUID | None] = ContextVar("aegis_tenant_id", default=None)
_current_trace: ContextVar[str | None] = ContextVar("aegis_trace_id", default=None)


@dataclass(frozen=True)
class Principal:
    """Authenticated caller. Background jobs use a system principal with an explicit tenant."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    label: str
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    # Location codes this principal may act on. ``None`` means unrestricted.
    location_scope: frozenset[str] | None = None
    session_id: uuid.UUID | None = None
    auth_method: str = "system"

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def in_scope(self, location_code: str) -> bool:
        return self.location_scope is None or location_code in self.location_scope


def system_principal(tenant_id: uuid.UUID, label: str = "system") -> Principal:
    from .security import ALL_PERMISSIONS

    return Principal(tenant_id=tenant_id, user_id=None, label=label, permissions=frozenset(ALL_PERMISSIONS))


def current_tenant() -> uuid.UUID | None:
    return _current_tenant.get()


def set_current_tenant(tenant_id: uuid.UUID | None):
    return _current_tenant.set(tenant_id)


def reset_current_tenant(token) -> None:
    _current_tenant.reset(token)


def current_trace_id() -> str | None:
    return _current_trace.get()


def set_trace_id(trace_id: str | None):
    return _current_trace.set(trace_id)


def reset_trace_id(token) -> None:
    _current_trace.reset(token)


def new_trace_id() -> str:
    return uuid.uuid4().hex


@dataclass
class JobContext:
    """Explicit context carried by background jobs (never rely on thread locals across workers)."""

    tenant_id: uuid.UUID
    trace_id: str = field(default_factory=new_trace_id)
