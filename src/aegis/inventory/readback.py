"""Fenced, bounded polling of an approved ERP/WMS cumulative execution contract."""
from __future__ import annotations
import uuid
from datetime import timedelta
from sqlalchemy import select, or_, exists
from ..core.db import admin_session, tenant_session
from ..core.config import get_settings
from ..core.errors import InvalidInput
from ..core.tenancy import system_principal
from ..core.util import utcnow
from ..models import IntegrationConnection, InventoryReservation, PlanLine, PlanRun, ExecutionObservation, WritebackCommand
from ..integration.connections import build_ctx
from ..integration.registry import get_connector
from ..integration.lease import heartbeat
from ..integration.streams import parse_datetime
from .execution import observe


def poll_execution_once(*, tenant_id=None):
    settings = get_settings()
    if not settings.plan_execution_enabled:
        return None
    now, owner = utcnow(), 'execution-poll:'+uuid.uuid4().hex
    with admin_session() as s:
        rows = s.execute(select(InventoryReservation, PlanLine, IntegrationConnection)
            .join(PlanLine, PlanLine.id == InventoryReservation.plan_line_id)
            .join(PlanRun, PlanRun.id == PlanLine.plan_run_id)
            .join(IntegrationConnection, IntegrationConnection.id == PlanRun.writeback_connection_id)
            .where(PlanRun.status == 'released', IntegrationConnection.status == 'active',
                InventoryReservation.tenant_id == tenant_id if tenant_id is not None else True,
                or_(InventoryReservation.next_poll_at.is_(None), InventoryReservation.next_poll_at <= now),
                or_(IntegrationConnection.lease_expires_at.is_(None), IntegrationConnection.lease_expires_at < now),
                ~exists(select(ExecutionObservation.id).where(ExecutionObservation.reservation_id == InventoryReservation.id, ExecutionObservation.is_final)))
            .order_by(InventoryReservation.next_poll_at.asc().nullsfirst(), InventoryReservation.id)
            .limit(50).with_for_update(of=[InventoryReservation, IntegrationConnection], skip_locked=True)).all()
        selected = None
        for reservation, line, conn in rows:
            reservation.next_poll_at = now+timedelta(seconds=60)
            if conn.config.get('execution_readback') and conn.is_mock == (settings.integration_mode == 'mock'):
                commands = list(s.scalars(select(WritebackCommand).where(WritebackCommand.plan_line_id == line.id)))
                if commands and all(c.status == 'confirmed' for c in commands):
                    spec = conn.config['execution_readback']
                    if len(commands)>1 and spec.get('identity', 'external_ref') != 'plan_line_id':
                        continue
                    ref = commands[0].external_ref if len(commands) == 1 else 'line:'+str(line.id)
                    conn.lease_owner, conn.lease_expires_at = owner, now+timedelta(seconds=settings.lease_seconds)
                    selected = (reservation.tenant_id, reservation.id, line.id, conn.id, ref)
                    break
        if selected is None:
            return None
    tenant, reservation_id, line_id, conn_id, ref = selected
    def renew():
        with tenant_session(tenant) as s:
            conn = s.get(IntegrationConnection, conn_id, with_for_update=True)
            if conn.lease_owner != owner or conn.lease_expires_at <= utcnow():
                raise InvalidInput('execution polling lease lost')
            conn.lease_expires_at = utcnow()+timedelta(seconds=settings.lease_seconds)
    try:
        with tenant_session(tenant) as s:
            conn = s.get(IntegrationConnection, conn_id)
            connector, ctx = get_connector(conn.kind), build_ctx(conn)
        with heartbeat(renew, settings.lease_seconds):
            report = connector.execution_report(ctx, ref, str(line_id))
            with tenant_session(tenant) as s:
                conn = s.get(IntegrationConnection, conn_id, with_for_update=True)
                if conn.lease_owner != owner or conn.lease_expires_at <= utcnow():
                    return 'lease_lost'
                if report is None:
                    return 'not_ready'
                required = {'source_version', 'external_ref', 'shipped', 'received', 'cancelled', 'is_final', 'occurred_at', 'evidence'}
                if set(report) != required or report['external_ref'] != ref or type(report['is_final']) is not bool or type(report['source_version']) is not int:
                    raise InvalidInput('execution readback contract or document identity mismatch')
                report['occurred_at'] = parse_datetime(report['occurred_at'], 'UTC')
                observe(s, system_principal(tenant, 'execution-readback'), reservation_id, connection_id=conn_id, **report)
        return 'observed'
    finally:
        with tenant_session(tenant) as s:
            conn = s.get(IntegrationConnection, conn_id, with_for_update=True)
            if conn.lease_owner == owner:
                conn.lease_owner = conn.lease_expires_at = None
