"""Durable ERP/WMS dispatch: an ambiguous create is always looked up before any retry."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.db import admin_session, tenant_session
from ..core.util import utcnow
from ..models import IntegrationConnection, PlanLine, WritebackCommand
from .connections import build_ctx
from .errors import AuthError, PermanentError, TransientError
from .outbox import emit
from .registry import get_connector
from .spi import WritebackRequest


@dataclass(frozen=True)
class Claim:
    id: uuid.UUID
    tenant_id: uuid.UUID
    owner: str
    lookup_first: bool


def claim_writeback(settings: Settings | None = None) -> Claim | None:
    settings = settings or get_settings()
    if not settings.writeback_enabled:
        return None
    with admin_session() as session:
        now = utcnow()
        cmd = session.scalar(select(WritebackCommand).join(IntegrationConnection, IntegrationConnection.id == WritebackCommand.connection_id)
            .where(WritebackCommand.status.in_(("pending", "sent_unconfirmed")), IntegrationConnection.status == "active",
                   or_(WritebackCommand.next_attempt_at.is_(None), WritebackCommand.next_attempt_at <= now),
                   or_(WritebackCommand.lease_expires_at.is_(None), WritebackCommand.lease_expires_at < now),
                   or_(IntegrationConnection.lease_expires_at.is_(None), IntegrationConnection.lease_expires_at < now))
            .order_by(WritebackCommand.created_at).limit(1).with_for_update(of=[WritebackCommand, IntegrationConnection], skip_locked=True))
        if cmd is None:
            return None
        lookup_first = cmd.status == "sent_unconfirmed"
        cmd.status, cmd.lease_owner = "sending", f"{settings.worker_id}:{uuid.uuid4().hex}"
        cmd.lease_expires_at = now + timedelta(seconds=settings.lease_seconds)
        cmd.attempts += 1
        # Sync and writeback share the connection lease / rate limiter ownership.
        conn = session.get(IntegrationConnection, cmd.connection_id)
        conn.lease_owner, conn.lease_expires_at = cmd.lease_owner, cmd.lease_expires_at
        return Claim(cmd.id, cmd.tenant_id, cmd.lease_owner, lookup_first)


def _execute_writeback(claim: Claim, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not settings.writeback_enabled:
        return "disabled"
    with tenant_session(claim.tenant_id) as session:
        cmd = session.get(WritebackCommand, claim.id)
        conn = session.get(IntegrationConnection, cmd.connection_id)
        request = WritebackRequest(cmd.command_type, cmd.idempotency_key, dict(cmd.payload))
        connector, ctx = get_connector(conn.kind), build_ctx(conn, settings)
    status, reference, error, delay = "confirmed", None, None, None
    auth_failed = False
    try:
        if claim.lookup_first:
            reference = connector.lookup(ctx, request)
            if reference is None:
                status = "sent_unconfirmed"  # Absence can be eventual consistency; never resubmit an ambiguous create.
        else:
            reference = connector.submit(ctx, request).external_ref
            if not reference:
                raise TransientError("empty external reference", maybe_delivered=True)
    except AuthError as exc:
        auth_failed = True
        status, error = "sent_unconfirmed" if claim.lookup_first else "pending", str(exc)
    except PermanentError as exc:
        status, error = "dead", str(exc)
    except TransientError as exc:
        status = "sent_unconfirmed" if claim.lookup_first or exc.maybe_delivered else "pending"
        error, delay = str(exc), exc.retry_after
    except Exception as exc:
        # Unknown failures may happen after the remote commit; be conservative.
        status, error = "sent_unconfirmed", f"{type(exc).__name__}: {exc}"
    with tenant_session(claim.tenant_id) as session:
        cmd = session.scalar(select(WritebackCommand).where(WritebackCommand.id == claim.id).with_for_update())
        if cmd.lease_owner != claim.owner or cmd.status != "sending" or cmd.lease_expires_at <= utcnow():
            return "lease_lost"
        conn = session.scalar(select(IntegrationConnection).where(IntegrationConnection.id == cmd.connection_id).with_for_update())
        if conn.lease_owner != claim.owner or conn.lease_expires_at <= utcnow():
            return "lease_lost"
        if status == "sent_unconfirmed" and cmd.attempts >= settings.writeback_max_attempts:
            status, error = "dead", error or "ambiguous delivery unresolved; operator reconciliation required; no resubmit"
        if error and cmd.attempts >= settings.writeback_max_attempts:
            status = "dead"
        if status == "pending" and cmd.attempts >= settings.writeback_max_attempts:
            status, error = "dead", error or "lookup did not resolve within retry budget"
        cmd.status, cmd.last_error = status, error[:2000] if error else None
        cmd.history = [*(cmd.history or []), {"at": utcnow().isoformat(), "status": status, "lookup": claim.lookup_first}]
        if auth_failed:
            conn.status = "auth_failed"
        if status == "confirmed":
            cmd.external_ref, cmd.confirmed_at = reference, utcnow()
        else:
            pause = delay if delay is not None else min(3600, settings.writeback_base_backoff_seconds * 2**min(cmd.attempts, 7))
            cmd.next_attempt_at = utcnow() + timedelta(seconds=pause)
        cmd.lease_owner = cmd.lease_expires_at = None
        conn.lease_owner = conn.lease_expires_at = None
        session.flush()
        if cmd.plan_line_id:
            line = session.scalar(select(PlanLine).where(PlanLine.id == cmd.plan_line_id).with_for_update())
            siblings = list(session.scalars(select(WritebackCommand).where(WritebackCommand.plan_line_id == line.id)))
            if all(c.status == "confirmed" for c in siblings):
                line.status = "confirmed"
            elif any(c.status == "dead" for c in siblings):
                line.status = "writeback_failed"
        emit(session, tenant_id=claim.tenant_id, topic=f"writeback.{status}", aggregate_type="writeback_command",
             aggregate_id=cmd.id, payload={"external_ref": reference, "error": cmd.last_error})
    return status


def sweep_writeback_leases(session: Session, now) -> int:
    commands = session.scalars(select(WritebackCommand).where(WritebackCommand.status == "sending", WritebackCommand.lease_expires_at < now)
                               .with_for_update(skip_locked=True)).all()
    for cmd in commands:
        # Crash between remote commit and local confirmation is an ambiguous delivery.
        cmd.status = "sent_unconfirmed"
        cmd.lease_owner = cmd.lease_expires_at = None
    return len(commands)


def run_writeback_once() -> str | None:
    claim = claim_writeback()
    return execute_writeback(claim) if claim else None


def execute_writeback(claim: Claim, settings: Settings | None = None) -> str:
    from .lease import heartbeat
    settings = settings or get_settings()
    def renew():
        with tenant_session(claim.tenant_id) as session:
            cmd = session.scalar(select(WritebackCommand).where(WritebackCommand.id == claim.id).with_for_update())
            if cmd.lease_owner != claim.owner or cmd.status != "sending" or cmd.lease_expires_at <= utcnow():
                raise RuntimeError("writeback lease lost")
            conn = session.scalar(select(IntegrationConnection).where(IntegrationConnection.id == cmd.connection_id).with_for_update())
            if conn.lease_owner != claim.owner or conn.lease_expires_at <= utcnow():
                raise RuntimeError("connection lease lost")
            cmd.lease_expires_at = conn.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
    with heartbeat(renew, settings.lease_seconds):
        return _execute_writeback(claim, settings)
