from fastapi.testclient import TestClient
from sqlalchemy import select
from aegis.core.db import tenant_session
from aegis.core.tenancy import system_principal
from aegis.models import PlanLine,IngestionBatch
from aegis.planning import lifecycle
def login(client,tenant):
    from aegis.models import Tenant
    with tenant_session(tenant) as s:slug=s.get(Tenant,tenant).slug
    r=client.post('/api/v1/auth/login',json={'tenant':slug,'email':'admin@test.local','password':'test-password-12345'})
    assert r.status_code==200,r.text
    client.headers['x-csrf-token']=r.json()['csrf_token']


def ready_plan(tenant):
    with tenant_session(tenant) as s:
        plan=lifecycle.request_plan(s,system_principal(tenant));id=plan.id
    assert lifecycle.execute_plan(lifecycle.claim_plan())=='pending_review'
    return id


def test_workspace_cookie_session_read_models_and_tenant_boundary(seeded,other_tenant):
    from aegis.api.app import create_app
    tenant,_,_=seeded;plan_id=ready_plan(tenant)
    with TestClient(create_app()) as client:
        assert client.get('/').status_code==200
        assert '/student/' not in client.get('/').text
        assert client.get('/api/v1/workspace/overview').status_code==401
        assert client.get('/workspace-assets/workspace.js').status_code==200
        assert client.get('/student/simulation/demo').status_code==404
        login(client,tenant)
        info=client.get('/api/v1/workspace/overview').json()
        assert info['tenant']['id']==str(tenant) and info['sku_count']==1
        assert sum(x['on_hand'] for x in info['stock'])==1001
        plan=client.get(f'/api/v1/plans/{plan_id}').json();line_id=plan['lines'][0]['id']
        forecast=client.get(f'/api/v1/workspace/plans/{plan_id}/forecast',params={'line_id':line_id,'horizon':7})
        assert forecast.status_code==200 and forecast.json()['forecast']['horizon_days']==7
        assert forecast.json()['input_digest']==plan['input_digest']
        assert client.get(f'/api/v1/workspace/plans/{plan_id}/evaluation').json()['metrics']['overall']['folds']>0
        assert client.get(f'/api/v1/workspace/plans/{plan_id}/execution').json()['reservations']==[]
        login(client,other_tenant)
        assert client.get('/api/v1/workspace/overview').json()['sku_count']==0
        for path in ('forecast','evaluation','execution'):
            response=client.get(f'/api/v1/workspace/plans/{plan_id}/{path}',params={'line_id':line_id})
            assert response.status_code==404


def test_workspace_permissions_csrf_and_connection_management(seeded):
    from aegis.api.app import create_app
    tenant,conn,_=seeded
    with TestClient(create_app()) as client:
        login(client,tenant)
        assert client.patch(f'/api/v1/connections/{conn}',json={'status':'paused'}).json()['status']=='paused'
        assert client.patch(f'/api/v1/connections/{conn}',json={'status':'active'}).json()['status']=='active'
        csrf=client.headers.pop('x-csrf-token')
        assert client.patch(f'/api/v1/connections/{conn}',json={'status':'paused'}).status_code==403
        client.headers['x-csrf-token']=csrf
        assert client.post('/api/v1/users',json={'email':'viewer@workspace.test','password':'viewer-pass-12345','roles':['VIEWER']}).status_code==201
        from aegis.models import Tenant
        with tenant_session(tenant) as s:slug=s.get(Tenant,tenant).slug
        response=client.post('/api/v1/auth/login',json={'tenant':slug,'email':'viewer@workspace.test','password':'viewer-pass-12345'})
        client.headers['x-csrf-token']=response.json()['csrf_token']
        assert client.get('/api/v1/workspace/catalog').status_code==200
        assert client.patch(f'/api/v1/connections/{conn}',json={'status':'paused'}).status_code==403
        assert client.post('/api/v1/plans',json={}).status_code==403


def test_workspace_http_approval_execution_settlement_and_close(seeded,tmp_path,monkeypatch):
    import csv,io
    from aegis.api.app import create_app
    from aegis.core.config import get_settings
    from aegis.core.util import utcnow
    from aegis.integration.connections import build_ctx
    from aegis.integration.connectors.file_csv import FileCsvConnector
    from aegis.integration.reconciliation import reconcile
    from aegis.integration.runtime import claim_sync_run,execute_sync_run
    from aegis.integration.spi import RawItem
    from aegis.models import IntegrationConnection
    tenant,conn,items=seeded;p=system_principal(tenant)
    monkeypatch.setattr(get_settings(),'file_inbox_root',tmp_path)
    with tenant_session(tenant) as s:
        for batch in s.scalars(select(IngestionBatch)):
            reconcile(s,p,batch.id,source_rows=[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in items if r.stream==batch.stream],scope={},evidence='independent HTTP test source')
    monkeypatch.setattr(get_settings(),'require_reconciliation',True)
    with TestClient(create_app()) as client:
        login(client,tenant)
        assert client.patch(f'/api/v1/connections/{conn}',json={'config':{'directory':str(tenant)}}).status_code==200
        created=client.post('/api/v1/plans',json={'mode':'execution'});assert created.status_code==202
        plan_id=created.json()['id']
        assert lifecycle.execute_plan(lifecycle.claim_plan())=='pending_review'
        plan=client.get('/api/v1/plans/'+plan_id).json();line=plan['lines'][0]
        assert client.post(f'/api/v1/plans/{plan_id}/lines/{line["id"]}/decision',json={'approve':True,'note':'HTTP E2E reviewed'}).status_code==200
        assert client.post(f'/api/v1/plans/{plan_id}/release',json={'note':'HTTP release reason'}).status_code==200
        from aegis.models import AuditLog
        with tenant_session(tenant) as s:
            audit=s.scalar(select(AuditLog).where(AuditLog.action=='plan.release'))
            assert audit.after['note']=='HTTP release reason'
        row=client.get(f'/api/v1/workspace/plans/{plan_id}/execution').json()['reservations'][0]
        reservation=row['reservation'];units=reservation['units'];ref='HTTP-E2E-WMS'
        assert client.post(f'/api/v1/inventory/reservations/{reservation["id"]}/execution',json={'connection_id':str(conn),'source_version':1,'external_ref':ref,'shipped':units,'received':units,'cancelled':0,'is_final':True,'occurred_at':utcnow().isoformat(),'evidence':'independent synthetic transfer'}).status_code==201
        assert client.post(f'/api/v1/plans/{plan_id}/actions/close',json={'note':'too early'}).status_code==409
        time=utcnow().isoformat();output=io.StringIO();writer=csv.writer(output)
        writer.writerow(['sku','location','as_of','on_hand','in_transit','allocated','backorder'])
        writer.writerow(['S','GLOBAL-POOL',time,1000-units,0,20,0]);writer.writerow(['S','W',time,1+units,0,3,0])
        uploaded=client.post(f'/api/v1/connections/{conn}/files/inventory_snapshots',files={'file':('inventory.csv',output.getvalue().encode(),'text/csv')})
        assert uploaded.status_code==201 and uploaded.json()['source_file_version']
        assert client.post(f'/api/v1/connections/{conn}/sync',json={'streams':['inventory_snapshots']}).status_code==202
        assert execute_sync_run(claim_sync_run())=='succeeded'
        batches=client.get('/api/v1/data/batches').json();batch=next(b for b in batches if b['stream']=='inventory_snapshots')
        with tenant_session(tenant) as s:
            source=[r for r in FileCsvConnector().read(build_ctx(s.get(IntegrationConnection,conn)),'inventory_snapshots',None,None) if isinstance(r,RawItem)]
            from aegis.core.util import json_safe
            manifest=json_safe({'source_rows':[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in source],'scope':batch['scope'],'evidence':'independently re-read CSV source','execution_refs':{ref:{'shipped':units,'received':units,'cancelled':0}}})
        result=client.post(f'/api/v1/data/batches/{batch["id"]}/reconcile',json=manifest)
        assert result.status_code==201 and result.json()['status']=='matched'
        assert client.post(f'/api/v1/inventory/reservations/{reservation["id"]}/settle',json={'source_batch_id':batch['id'],'destination_batch_id':batch['id'],'evidence':'both authoritative stock positions reflect transfer'}).status_code==201
        assert client.post(f'/api/v1/plans/{plan_id}/actions/close',json={'note':'all units reconciled'}).status_code==200
        assert client.get('/api/v1/plans/'+plan_id).json()['status']=='closed'
