from datetime import timedelta

from sqlalchemy import select
from aegis.core.db import tenant_session
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.integration.connections import create_connection
from aegis.integration.errors import TransientError
from aegis.integration.registry import register
from aegis.integration.spi import SubmitResult
from aegis.integration.writeback import claim_writeback, execute_writeback, sweep_writeback_leases
from aegis.models import PlanLine, WritebackCommand
from aegis.planning import lifecycle


@register
class FaultingERP:
    kind = "test-erp"
    version = "1"
    calls = 0
    remote = {}
    def validate_config(self, config, settings=None):
        return config
    def streams(self, config):
        return []
    def writeback_commands(self, config):
        return ["create_transfer"]
    def submit(self, ctx, request):
        type(self).calls += 1
        type(self).remote[request.idempotency_key] = "PO-remote"
        raise TransientError("response lost after remote commit", maybe_delivered=True)
    def lookup(self, ctx, request):
        return type(self).remote.get(request.idempotency_key)


def test_ambiguous_remote_commit_is_looked_up_without_duplicate_create(seeded, monkeypatch):
    from aegis.core.config import get_settings
    monkeypatch.setattr(get_settings(), "writeback_enabled", True)
    tenant, _, _ = seeded
    FaultingERP.calls, FaultingERP.remote = 0, {}
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        conn = create_connection(s, p, name="faulting-erp", kind="test-erp", config={})
        plan = lifecycle.request_plan(s, p, writeback_connection_id=conn.id, mode="execution")
        plan_id = plan.id
    claim = lifecycle.claim_plan()
    assert lifecycle.execute_plan(claim) == "pending_review"
    with tenant_session(tenant) as s:
        line = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == plan_id))
        lifecycle.decide_line(s, p, plan_id, line.id, approve=True)
        lifecycle.release_plan(s, p, plan_id)
        lifecycle.release_plan(s, p, plan_id)
    claim = claim_writeback()
    assert execute_writeback(claim) == "sent_unconfirmed"
    with tenant_session(tenant) as s:
        cmd = s.get(WritebackCommand, claim.id)
        cmd.next_attempt_at = utcnow()-timedelta(seconds=1)
    retry = claim_writeback()
    assert retry.lookup_first
    assert execute_writeback(retry) == "confirmed"
    assert FaultingERP.calls == 1
    with tenant_session(tenant) as s:
        command = s.get(WritebackCommand, retry.id)
        assert command.external_ref == "PO-remote"
        line = s.get(PlanLine, command.plan_line_id)
        assert line.status == "confirmed"
        assert s.scalar(select(WritebackCommand).where(WritebackCommand.plan_run_id == plan_id)).id == command.id


def test_absent_lookup_never_resubmits_ambiguous_create_and_stops_at_budget(seeded, monkeypatch):
    from aegis.core.config import get_settings
    settings=get_settings()
    monkeypatch.setattr(settings,'writeback_enabled',True)
    monkeypatch.setattr(settings,'writeback_max_attempts',2)
    tenant,_,_=seeded;p=system_principal(tenant)
    FaultingERP.calls,FaultingERP.remote=0,{}
    with tenant_session(tenant) as s:
        conn=create_connection(s,p,name='invisible-erp',kind='test-erp',config={})
        plan=lifecycle.request_plan(s,p,writeback_connection_id=conn.id,mode='execution');plan_id=plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan())=='pending_review'
    with tenant_session(tenant) as s:
        line=s.scalar(select(PlanLine).where(PlanLine.plan_run_id==plan_id))
        lifecycle.decide_line(s,p,plan_id,line.id,approve=True)
        lifecycle.release_plan(s,p,plan_id)
    first=claim_writeback();assert execute_writeback(first)=='sent_unconfirmed'
    FaultingERP.remote.clear()  # Query endpoint is eventually consistent / unavailable evidence.
    with tenant_session(tenant) as s:s.get(WritebackCommand,first.id).next_attempt_at=utcnow()-timedelta(seconds=1)
    retry=claim_writeback();assert retry.lookup_first
    assert execute_writeback(retry)=='dead'
    assert FaultingERP.calls==1
