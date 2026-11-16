import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select
from aegis.api.app import create_app
from aegis.core.db import tenant_session
from aegis.core.tenancy import system_principal
from aegis.models import AppUser, Tenant, SourceReconciliation, IngestionBatch, IdMapping, RawRecord, QuarantineRecord
from aegis.integration.reconciliation import reconcile
from aegis.integration.raw import land_batch
from aegis.integration.spi import RawItem
from aegis.integration.normalize import normalize_batch
from aegis.core.util import utcnow


def login(client,tenant,email='admin@test.local',password='test-password-12345'):
    with tenant_session(tenant) as s:slug=s.get(Tenant,tenant).slug
    response=client.post('/api/v1/auth/login',json={'tenant':slug,'email':email,'password':password})
    assert response.status_code==200,response.text
    client.headers['x-csrf-token']=response.json()['csrf_token']


def check_fixture(seeded):
    tenant,_,items=seeded
    with tenant_session(tenant) as s:
        batch=s.scalar(select(IngestionBatch).where(IngestionBatch.stream=='inventory_snapshots'))
        rows=[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in items if r.stream==batch.stream]
        bad=[{**r,'payload':{**r['payload'],'on_hand':r['payload']['on_hand']+1}} for r in rows]
        bad[0]['payload']['customer_email']='private@fixture.test'
        row=reconcile(s,system_principal(tenant),batch.id,source_rows=bad,scope=batch.scope,evidence='independent mismatched export')
        return batch.id,row.id,rows


def test_reconciliation_disposition_requires_current_evidence_and_reopens(seeded,other_tenant):
    tenant,_,_=seeded;batch,check_id,rows=check_fixture(seeded)
    with TestClient(create_app()) as c:
        login(c,tenant)
        detail=c.get(f'/api/v1/data/reconciliations/{check_id}').json()
        assert detail['check']['details']['changed_count']==2
        assert 'private@fixture.test' not in str(detail['check']['details'])
        assert 'customer_email' in str(detail['check']['details']['samples'])  # field name only
        body={'status':'in_progress','expected_revision':0,'note':'source export needs correction'}
        case=c.post(f'/api/v1/data/batches/{batch}/disposition',json=body)
        assert case.status_code==200,case.text
        assert c.post(f'/api/v1/data/batches/{batch}/disposition',json=body).status_code==409
        assert c.post(f'/api/v1/data/batches/{batch}/disposition',json={'status':'resolved','expected_revision':1,'note':'cannot waive'}).status_code==409
        from aegis.core.util import json_safe
        result=c.post(f'/api/v1/data/batches/{batch}/reconcile',json=json_safe({'source_rows':rows,'scope':{},'evidence':'correct independent source export'}))
        assert result.status_code==201,result.text
        malformed={**rows[0],'source_updated_at':'invalid-date'}
        assert c.post(f'/api/v1/data/batches/{batch}/reconcile',json=json_safe({'source_rows':[malformed],'scope':{},'evidence':'invalid version rejected'})).status_code==422
        passed=result.json()['id']
        resolved=c.post(f'/api/v1/data/batches/{batch}/disposition',json={'status':'resolved','expected_revision':1,'resolution_check_id':passed,'note':'source proof corrected'})
        assert resolved.status_code==200,resolved.text
        assert c.get(f'/api/v1/data/reconciliations/{passed}').json()['case']['status']=='resolved'
        latest=c.get('/api/v1/workspace/reconciliations').json()
        assert latest['total']==1 and latest['items'][0]['status']=='matched'
        _,new_bad,_=check_fixture(seeded)
        assert c.get(f'/api/v1/data/reconciliations/{new_bad}').json()['case']['status']=='open'
        login(c,other_tenant)
        assert c.get(f'/api/v1/data/reconciliations/{passed}').status_code==404
        assert c.post(f'/api/v1/data/batches/{batch}/disposition',json=body).status_code==404


def test_mapping_confirm_worker_replay_and_tenant_bound_target(seeded,other_tenant):
    tenant,conn,_=seeded
    with tenant_session(tenant) as s:
        land_batch(s,tenant_id=tenant,connection_id=conn,sync_run_id=None,items=[RawItem('daily_demand','external-demand',{'sku':'EXT-S','location':'W','date':utcnow().date().isoformat(),'units':2},utcnow())])
        assert normalize_batch(s,tenant).quarantined==1
        mapping=s.scalar(select(IdMapping).where(IdMapping.external_id=='EXT-S'));mapping_id=mapping.id
    with TestClient(create_app()) as c:
        login(c,tenant)
        result=c.get('/api/v1/workspace/mappings',params={'q':'EXT-S','status':'pending'}).json()
        assert result['total']==1
        row=result['items'][0]
        body={'target_code':'S','note':'catalog independently checked','expected_updated_at':row['updated_at']}
        assert c.get('/api/v1/workspace/catalog/search?entity=sku&q=S').json()[0]['code']=='S'
        bad={**body,'target_code':'OTHER-TENANT-ONLY'}
        assert c.post(f'/api/v1/mappings/{mapping_id}/resolve',json=bad).status_code==422
        resolved=c.post(f'/api/v1/mappings/{mapping_id}/resolve',json=body)
        assert resolved.status_code==200 and resolved.json()['requeued']==1
        with tenant_session(tenant) as s:
            assert normalize_batch(s,tenant).normalized==1
            assert s.scalar(select(RawRecord).where(RawRecord.external_id=='external-demand')).normalize_status=='ok'
        assert c.post(f'/api/v1/mappings/{mapping_id}/resolve',json=body).status_code==409
        login(c,other_tenant)
        assert c.post(f'/api/v1/mappings/{mapping_id}/resolve',json=body).status_code==404


def test_user_permissions_revocation_last_admin_and_stale_update(tenant,other_tenant):
    with TestClient(create_app()) as admin,TestClient(create_app()) as member:
        login(admin,tenant)
        created=admin.post('/api/v1/users',json={'email':'planner@fixture.test','password':'a-long-test-password','roles':['PLANNER']})
        assert created.status_code==201,created.text
        user=created.json();assert 'password_hash' not in user
        login(member,tenant,'planner@fixture.test','a-long-test-password')
        assert member.get('/api/v1/users').status_code==403
        assert member.get('/api/v1/roles').status_code==403
        user=next(u for u in admin.get('/api/v1/users').json()['items'] if u['id']==user['id'])
        patch={'expected_updated_at':user['updated_at'],'roles':['VIEWER'],'note':'least privilege'}
        changed=admin.patch(f'/api/v1/users/{user["id"]}',json=patch)
        assert changed.status_code==200,changed.text
        assert member.get('/api/v1/auth/me').status_code==401
        assert admin.patch(f'/api/v1/users/{user["id"]}',json=patch).status_code==409
        users=admin.get('/api/v1/users').json()['items'];owner=next(u for u in users if 'TENANT_ADMIN' in u['roles'])
        assert admin.patch(f'/api/v1/users/{owner["id"]}',json={'expected_updated_at':owner['updated_at'],'is_active':False,'note':'deny last admin lockout'}).status_code==409
        assert admin.patch(f'/api/v1/users/{user["id"]}',json={'expected_updated_at':changed.json()['updated_at'],'location_scope':['INVALID'],'note':'reject unknown scope'}).status_code==422
        login(member,tenant,'planner@fixture.test','a-long-test-password')
        assert member.post('/api/v1/users',json={'email':'evil@fixture.test','password':'password-long-enough','roles':['TENANT_ADMIN']}).status_code==403
        assert admin.post(f'/api/v1/users/{user["id"]}/revoke-sessions',json={'note':'incident review'}).status_code==200
        assert member.get('/api/v1/auth/me').status_code==401
        login(admin,other_tenant)
        assert admin.patch(f'/api/v1/users/{user["id"]}',json=patch).status_code==404
        assert admin.post(f'/api/v1/users/{user["id"]}/revoke-sessions',json={'note':'cross tenant rejected'}).status_code==404


def test_google_member_creation_and_role_contract(tenant,monkeypatch):
    from aegis.core.config import get_settings
    with TestClient(create_app()) as c:
        login(c,tenant)
        monkeypatch.setattr(get_settings(),'auth_mode','google')
        result=c.post('/api/v1/users',json={'email':'google@fixture.test','roles':['VIEWER'],'google_subject':'verified-fixture-google-sub'})
        assert result.status_code==201,result.text
        users=c.get('/api/v1/users?q=google').json()
        assert users['total']==1 and users['items'][0]['google_bound']
        assert 'subject' not in str(users)
        assert c.post('/api/v1/users',json={'email':'unbound-admin@fixture.test','roles':['TENANT_ADMIN']}).status_code==201
        owner=next(u for u in c.get('/api/v1/users').json()['items'] if u['email']=='admin@test.local')
        assert c.patch('/api/v1/users/'+owner['id'],json={'expected_updated_at':owner['updated_at'],'is_active':False,'note':'unbound replacement is not usable'}).status_code==409
        assert c.get('/api/v1/roles').json()['VIEWER']==['data:view','integration:connection:view','plan:view']


def test_concurrent_admin_demotions_keep_one_active_admin(tenant):
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(create_app()) as a,TestClient(create_app()) as b:
        login(a,tenant)
        made=a.post('/api/v1/users',json={'email':'second-admin@fixture.test','password':'second-admin-password','roles':['TENANT_ADMIN']})
        assert made.status_code==201
        login(b,tenant,'second-admin@fixture.test','second-admin-password')
        users=a.get('/api/v1/users').json()['items']
        owner=next(u for u in users if u['email']=='admin@test.local')
        second=next(u for u in users if u['email']=='second-admin@fixture.test')
        def demote(client,user):return client.patch('/api/v1/users/'+user['id'],json={'roles':['VIEWER'],'expected_updated_at':user['updated_at'],'note':'concurrent demotion regression'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(demote,a,owner);last=pool.submit(demote,b,second)
            assert sorted([first.result(),last.result()])==[200,409]
        with tenant_session(tenant) as s:
            active=[u for u in s.scalars(select(AppUser)) if u.is_active and 'TENANT_ADMIN' in u.roles]
            assert len(active)==1
