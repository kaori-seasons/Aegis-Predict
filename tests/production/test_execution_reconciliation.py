from __future__ import annotations
from datetime import timedelta
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from aegis.core.db import tenant_session
from aegis.core.errors import Conflict, InvalidInput
from aegis.core.tenancy import system_principal
from aegis.core.util import utcnow
from aegis.integration.batches import start_batch, finish_landing
from aegis.integration.raw import land_batch
from aegis.integration.normalize import normalize_batch
from aegis.integration.reconciliation import reconcile, require_reconciled
from aegis.integration.spi import RawItem
from aegis.inventory.execution import observe, settle
from aegis.inventory.service import held_units
from aegis.models import IngestionBatch, InventoryReservation, Location, PlanLine, PlanRun
from aegis.planning import lifecycle


def released(seeded):
    tenant, conn, _ = seeded
    p = system_principal(tenant)
    with tenant_session(tenant) as s:
        plan = lifecycle.request_plan(s, p, mode='execution'); plan_id = plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan()) == 'pending_review'
    with tenant_session(tenant) as s:
        line = s.scalar(select(PlanLine).where(PlanLine.plan_run_id == plan_id))
        lifecycle.decide_line(s, p, plan_id, line.id, approve=True)
        lifecycle.release_plan(s, p, plan_id)
        row = s.scalar(select(InventoryReservation).where(InventoryReservation.plan_line_id == line.id))
        return tenant, conn, p, plan_id, row.id, row.units


def test_equal_count_changed_source_is_a_mismatch_and_evidence_is_immutable(seeded):
    tenant, conn, items = seeded; p=system_principal(tenant)
    with tenant_session(tenant) as s:
        batch=s.scalar(select(IngestionBatch).where(IngestionBatch.stream=='inventory_snapshots'))
        rows=[dict(external_id=r.external_id, payload=r.payload, source_updated_at=r.source_updated_at) for r in items if r.stream=='inventory_snapshots']
        rows[0]={'external_id': rows[0]['external_id'], 'payload': {**rows[0]['payload'], 'on_hand': 999}, 'source_updated_at': rows[0]['source_updated_at']}
        check=reconcile(s,p,batch.id,source_rows=rows,scope={},evidence='independent source export')
        assert check.status=='mismatch' and check.details['changed_count']==1
        with pytest.raises(Conflict): require_reconciled(s,tenant)
        check_id=check.id
    with pytest.raises(DBAPIError):
        with tenant_session(tenant) as s: s.execute(text('DELETE FROM source_reconciliation WHERE id=:id'),{'id':check_id})


def test_partial_dispatch_cancel_receive_settle_close_and_next_plan(seeded):
    tenant,conn,p,plan_id,res_id,units=released(seeded)
    moment=utcnow()
    base=dict(connection_id=conn,external_ref='WMS-TRANSFER-1',occurred_at=moment,evidence='independent WMS readback')
    with tenant_session(tenant) as s:
        event=observe(s,p,res_id,source_version=1,shipped=10,received=0,cancelled=0,is_final=False,**base)
        assert observe(s,p,res_id,source_version=1,shipped=10,received=0,cancelled=0,is_final=False,**base).id==event.id
        row=s.get(InventoryReservation,res_id)
        assert held_units(s,tenant,row.sku_id,row.location_id)==units
        with pytest.raises(Conflict): observe(s,p,res_id,source_version=2,shipped=9,received=0,cancelled=0,is_final=False,**base)
        with pytest.raises(Conflict): lifecycle.terminate_plan(s,p,plan_id,action='close',note='premature')
        observe(s,p,res_id,source_version=2,shipped=units-5,received=units-5,cancelled=5,is_final=True,**base)
        assert held_units(s,tenant,row.sku_id,row.location_id)==units-5
        with pytest.raises(Conflict): settle(s,p,res_id,source_batch_id=row.snapshot_id,evidence='missing proof')
    # Both independently exported stock and execution counters explicitly include the transfer.
    now=utcnow()
    inventory=[RawItem('inventory_snapshots','settled-pool',{'sku':'S','location':'GLOBAL-POOL','as_of':now.isoformat(),'on_hand':1000-(units-5),'allocated':20},now),
        RawItem('inventory_snapshots','settled-W',{'sku':'S','location':'W','as_of':now.isoformat(),'on_hand':1+(units-5),'allocated':3},now)]
    with tenant_session(tenant) as s:
        batch=start_batch(s,tenant,conn,'inventory_snapshots')
        land_batch(s,tenant_id=tenant,connection_id=conn,sync_run_id=None,items=inventory,batch_id=batch.id)
        finish_landing(s,batch.id); normalize_batch(s,tenant)
        assert batch.status=='complete'
        reconcile(s,p,batch.id,source_rows=[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in inventory],scope={},evidence='WMS stock export',execution_refs={'WMS-TRANSFER-1':{'shipped':units-5,'received':units-5,'cancelled':5}})
        settlement=settle(s,p,res_id,source_batch_id=batch.id,destination_batch_id=batch.id,evidence='transfer reflected at both ends')
        assert settle(s,p,res_id,source_batch_id=batch.id,destination_batch_id=batch.id,evidence='retry').id==settlement.id
        row=s.get(InventoryReservation,res_id)
        assert held_units(s,tenant,row.sku_id,row.location_id)==0
        lifecycle.terminate_plan(s,p,plan_id,action='close',note='all units accounted for')
        next_plan=lifecycle.request_plan(s,p)
        lifecycle.terminate_plan(s,p,next_plan.id,action='cancel',note='test cleanup')


def test_reject_over_execution_and_cross_tenant(seeded, other_tenant):
    tenant,conn,p,plan_id,res_id,units=released(seeded)
    data=dict(connection_id=conn,source_version=1,external_ref='WMS-X',shipped=units+1,received=0,cancelled=0,is_final=False,occurred_at=utcnow(),evidence='test')
    with tenant_session(tenant) as s:
        with pytest.raises(Conflict): observe(s,p,res_id,**data)
    with tenant_session(other_tenant) as s:
        with pytest.raises(InvalidInput): observe(s,system_principal(other_tenant),res_id,**data)
