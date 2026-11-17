from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import AppUser, Tenant, UserSession
from .config import get_settings
from .errors import InvalidInput, Unauthenticated
from .tenancy import Principal
from .util import utcnow

# ---------------------------------------------------------------- permissions
# Action-level permission codes (agentic-aps ``@RequirePermission`` style).
USER_MANAGE = "user:manage"
CONNECTION_VIEW = "integration:connection:view"
CONNECTION_EDIT = "integration:connection:edit"
SYNC_RUN = "integration:sync:run"
BACKFILL_RUN = "integration:backfill:run"
MAPPING_RESOLVE = "integration:mapping:resolve"
DATA_VIEW = "data:view"
POLICY_EDIT = "policy:edit"
PLAN_VIEW = "plan:view"
PLAN_GENERATE = "plan:generate"
PLAN_ADJUST = "plan:adjust"
PLAN_APPROVE = "plan:approve"
PLAN_RELEASE = "plan:release"
AUDIT_VIEW = "audit:view"
FORECAST_RUN = 'forecast:run'
FORECAST_APPROVE = 'forecast:approve'
BOM_EDIT = 'bom:edit'
BOM_APPROVE = 'bom:approve'
RISK_MANAGE = 'risk:manage'
OVERRIDE_EDIT = 'forecast:override:edit'
OVERRIDE_APPROVE = 'forecast:override:approve'


ALL_PERMISSIONS = frozenset(
    {
        FORECAST_RUN, FORECAST_APPROVE, BOM_EDIT, BOM_APPROVE, RISK_MANAGE, OVERRIDE_EDIT, OVERRIDE_APPROVE,
        USER_MANAGE,
        CONNECTION_VIEW,
        CONNECTION_EDIT,
        SYNC_RUN,
        BACKFILL_RUN,
        MAPPING_RESOLVE,
        DATA_VIEW,
        POLICY_EDIT,
        PLAN_VIEW,
        PLAN_GENERATE,
        PLAN_ADJUST,
        PLAN_APPROVE,
        PLAN_RELEASE,
        AUDIT_VIEW,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "TENANT_ADMIN": ALL_PERMISSIONS,
    "INTEGRATION_ADMIN": frozenset(
        {CONNECTION_VIEW, CONNECTION_EDIT, SYNC_RUN, BACKFILL_RUN, MAPPING_RESOLVE, DATA_VIEW, PLAN_VIEW, AUDIT_VIEW}
    ),
    "PLANNER": frozenset({CONNECTION_VIEW, DATA_VIEW, POLICY_EDIT, PLAN_VIEW, PLAN_GENERATE, PLAN_ADJUST, FORECAST_RUN, BOM_EDIT, RISK_MANAGE, OVERRIDE_EDIT}),
    "APPROVER": frozenset({DATA_VIEW, PLAN_VIEW, PLAN_APPROVE, PLAN_RELEASE, FORECAST_APPROVE, BOM_APPROVE, RISK_MANAGE, OVERRIDE_APPROVE}),
    "VIEWER": frozenset({CONNECTION_VIEW, DATA_VIEW, PLAN_VIEW}),
}
ROLES = frozenset(ROLE_PERMISSIONS)


def permissions_for(roles: list[str] | frozenset[str]) -> frozenset[str]:
    granted: set[str] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted(set(roles) - ROLES)
    if unknown:
        raise InvalidInput(f"unknown roles: {', '.join(unknown)}")
    return sorted(set(roles))


# ---------------------------------------------------------------- passwords
_hasher = PasswordHasher()
MIN_PASSWORD_LENGTH = 12
# Verified against when the user does not exist so timing does not reveal valid accounts.
_DUMMY_HASH = _hasher.hash("aegis-dummy-password-for-timing")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidInput(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# ---------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value.encode("utf-8"), header_value.encode("utf-8"))


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    expires_in_seconds: int


def authenticate(
    session: Session,
    tenant: Tenant,
    email: str,
    password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> IssuedSession | None:
    """Verify credentials inside a tenant-bound session and issue an opaque session token.

    Returns ``None`` on failure *without raising*, so the caller commits the failure counter /
    lockout before answering with one generic error for unknown users, wrong passwords and
    locked accounts.
    """

    settings = get_settings()
    now = utcnow()
    user = session.scalar(
        select(AppUser).where(AppUser.tenant_id == tenant.id, AppUser.email == email.strip().lower()).with_for_update()
    )
    if user is None or tenant.status != "active":
        verify_password(_DUMMY_HASH, password)
        return None
    if not user.is_active or (user.locked_until is not None and user.locked_until > now):
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_count = 0
        return None

    if needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    return issue_session(session, user, ip=ip, user_agent=user_agent)


def issue_session(session: Session, user: AppUser, *, ip=None, user_agent=None) -> IssuedSession:
    settings = get_settings()
    now = utcnow()
    user.last_login_at = now
    token = secrets.token_urlsafe(32)
    ttl = timedelta(minutes=settings.session_ttl_minutes)
    record = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=_token_hash(token),
        expires_at=now + ttl,
        ip=ip,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(record)
    session.flush()
    return IssuedSession(
        token=token,
        session_id=record.id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        expires_in_seconds=int(ttl.total_seconds()),
    )


def lookup_session(session: Session, token: str) -> UserSession:
    """Resolve a session token (global session: user_session is not under RLS)."""

    now = utcnow()
    record = session.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        raise Unauthenticated("session expired or revoked")
    if record.last_seen_at is None or (now - record.last_seen_at) > timedelta(minutes=1):
        record.last_seen_at = now
    return record


def load_principal(session: Session, user_session: UserSession, auth_method: str) -> Principal:
    """Load the user inside a tenant session; deactivation takes effect on the next request."""

    user = session.get(AppUser, user_session.user_id)
    if user is None or not user.is_active or user.tenant_id != user_session.tenant_id:
        raise Unauthenticated("user is disabled")
    return Principal(
        tenant_id=user.tenant_id,
        user_id=user.id,
        label=user.email,
        roles=frozenset(user.roles),
        permissions=permissions_for(user.roles),
        location_scope=frozenset(user.location_scope) if user.location_scope is not None else None,
        session_id=user_session.id,
        auth_method=auth_method,
    )


def revoke_session(session: Session, session_id: uuid.UUID) -> None:
    session.execute(
        update(UserSession).where(UserSession.id == session_id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow())
    )


def revoke_user_sessions(session: Session, user_id: uuid.UUID) -> None:
    session.execute(
        update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow())
    )
