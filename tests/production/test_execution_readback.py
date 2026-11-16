import httpx
import pytest
from sqlalchemy import select,func
from aegis.core.config import get_settings
from aegis.core.db import tenant_session
from aegis.core.errors import InvalidInput
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.integration.connections import create_connection
from aegis.integration.connectors.generic_rest import GenericRestConnector
from aegis.inventory.readback import poll_execution_once
from aegis.models import ExecutionObservation,InventoryReservation,PlanLine,WritebackCommand
from aegis.planning import lifecycle


def test_fenced_generic_wms_readback_rejects_identity_mismatch_and_is_idempotent(seeded,monkeypatch):
    tenant,_,_=seeded;p=system_principal(tenant)
    monkeypatch.setattr(get_settings(),'writeback_enabled',True)
    config={'base_url':'https://warehouse.example.com','streams':{'skus':{'path':'/skus'}},
        'writeback':{'commands':{'create_transfer':{'path':'/transfers','body_field_map':{'ref':'$idempotency_key'}}},
                    'lookup':{'path':'/transfers'}},'execution_readback':{'path':'/execution'}}
    with tenant_session(tenant) as s:
        conn=create_connection(s,p,name='readback-wms',kind='generic_rest',config=config)
        plan=lifecycle.request_plan(s,p,mode='execution',writeback_connection_id=conn.id);plan_id=plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan())=='pending_review'
    with tenant_session(tenant) as s:
        line=s.scalar(select(PlanLine).where(PlanLine.plan_run_id==plan_id))
        lifecycle.decide_line(s,p,plan_id,line.id,approve=True);lifecycle.release_plan(s,p,plan_id)
        s.flush()
        cmd=s.scalar(select(WritebackCommand).where(WritebackCommand.plan_line_id==line.id))
        cmd.status='confirmed';cmd.external_ref='WMS-DOCUMENT';cmd.confirmed_at=utcnow()
        reservation=s.scalar(select(InventoryReservation).where(InventoryReservation.plan_line_id==line.id));reservation_id=reservation.id
    data=dict(source_version=1,external_ref='WRONG-DOCUMENT',shipped=10,received=0,cancelled=0,is_final=False,occurred_at=utcnow().isoformat(),evidence='WMS read-only endpoint')
    from aegis.integration.connectors import generic_rest
    original_guard = generic_rest.guard_url
    monkeypatch.setattr(generic_rest, 'guard_url', lambda url, settings, resolve: original_guard(url, settings, resolve=False))
    monkeypatch.setattr(GenericRestConnector,'_client',lambda _,config:httpx.Client(base_url=config.base_url,transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':data}))))
    with pytest.raises(InvalidInput,match='identity mismatch'):poll_execution_once(tenant_id=tenant)
    with tenant_session(tenant) as s:
        assert not s.scalar(select(func.count()).select_from(ExecutionObservation).where(ExecutionObservation.reservation_id==reservation_id))
        s.get(InventoryReservation,reservation_id).next_poll_at=utcnow()
    data['external_ref']='WMS-DOCUMENT'
    assert poll_execution_once(tenant_id=tenant)=='observed'
    with tenant_session(tenant) as s:s.get(InventoryReservation,reservation_id).next_poll_at=utcnow()
    assert poll_execution_once(tenant_id=tenant)=='observed'
    with tenant_session(tenant) as s:
        assert s.scalar(select(func.count()).select_from(ExecutionObservation).where(ExecutionObservation.reservation_id==reservation_id))==1
        assert s.get(InventoryReservation,reservation_id).consumed_units==10
