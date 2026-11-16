"""Local-only E2E harness. Fake Google issuer transport is NEVER installed by aegis serve.

Run against an explicit disposable *_test database. The browser traverses the actual
Google start/callback, cookie, CSRF, API and Worker paths; no browser cookie injection.
"""
from __future__ import annotations
import argparse,base64,csv,json,os,secrets,threading,time,uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding,rsa
from sqlalchemy import create_engine,select,text
from sqlalchemy.engine import make_url


def seed(tenant, root=None):
    from aegis.core.db import tenant_session
    from aegis.core.tenancy import system_principal
    from aegis.core.util import utcnow
    from aegis.integration.authority import set_authority
    from aegis.integration.batches import start_batch,finish_landing
    from aegis.integration.connections import create_connection
    from aegis.integration.normalize import normalize_batch
    from aegis.integration.raw import land_batch
    from aegis.integration.reconciliation import reconcile
    from aegis.integration.spi import RawItem
    from aegis.integration.streams import ordered_streams
    from aegis.models import Location
    now=utcnow();p=system_principal(tenant,'e2e-fixture')
    values=[('skus','S',{'sku':'S','name':'USB-C 快充线','pack_size':5,'unit_margin':10,'service_class':'standard','category':'3C配件'}),
        ('locations','GLOBAL-POOL',{'code':'GLOBAL-POOL','name':'深圳中心库存池','kind':'pool','timezone':'UTC'}),
        ('locations','W',{'code':'W','name':'洛杉矶仓','country':'US','kind':'warehouse','timezone':'America/Los_Angeles','handling_capacity_per_day':1000,'capacity_units':10000,'max_daily_inbound_units':1000}),
        ('lanes','pool-W',{'from':'GLOBAL-POOL','to':'W','lead_time_days':10,'lead_time_std_days':2,'unit_cost':1}),
        ('transport_options','sea',{'from':'GLOBAL-POOL','to':'W','mode':'SEA','lead_time_days':10,'unit_cost':1,'reliability':1,'max_units':1000}),
        ('inventory_snapshots','S-pool',{'sku':'S','location':'GLOBAL-POOL','as_of':now.isoformat(),'on_hand':1000,'allocated':20}),
        ('inventory_snapshots','S-W',{'sku':'S','location':'W','as_of':now.isoformat(),'on_hand':1})]
    values += [('daily_demand',str(i),{'sku':'S','location':'W','date':(now.date()-timedelta(days=61-i)).isoformat(),'units':10,'stockout':False}) for i in range(60)]
    with tenant_session(tenant) as s:
        conn=create_connection(s,p,name='Mock仓库CSV',kind='file_csv',config={'directory':str(tenant)})
        for stream in ordered_streams({v[0] for v in values}):
            items=[RawItem(a,b,c,now) for a,b,c in values if a==stream]
            batch=start_batch(s,tenant,conn.id,stream)
            land_batch(s,tenant_id=tenant,connection_id=conn.id,sync_run_id=None,items=items,batch_id=batch.id)
            finish_landing(s,batch.id);assert normalize_batch(s,tenant).quarantined==0
            if root and stream=='inventory_snapshots':
                data={'source_rows':[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at.isoformat()) for r in items],'scope':{},'evidence':'Independent initial synthetic source export'}
                (root/'governance-manifest.json').write_text(json.dumps(data))
            reconcile(s,p,batch.id,source_rows=[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in items],scope={},evidence='Independent synthetic E2E source fixture')
        for location in s.scalars(select(Location)):
            set_authority(s,p,domain='inventory',location_id=location.id,connection_id=conn.id,stream='inventory_snapshots',note='E2E synthetic source contract')
            if location.kind!='pool':set_authority(s,p,domain='demand',location_id=location.id,connection_id=conn.id,stream='daily_demand',note='E2E daily totals')
        return conn.id


def main():
    p=argparse.ArgumentParser();p.add_argument('--database-url',required=True);p.add_argument('--port',type=int,default=8020);p.add_argument('--state',type=Path,default=Path('/tmp/aegis-workspace-e2e.json'));args=p.parse_args()
    url=make_url(args.database_url)
    if not (url.database or '').endswith('_test') or url.host not in ('127.0.0.1','localhost'):raise SystemExit('local disposable *_test database required')
    root=Path('/tmp/aegis-workspace-e2e-'+secrets.token_hex(4));root.mkdir()
    os.environ.update(AEGIS_ADMIN_DATABASE_URL=args.database_url,AEGIS_DATABASE_URL=url.set(username='aegis_app',password=None).render_as_string(hide_password=False),AEGIS_ENV='test',AEGIS_COOKIE_SECURE='false',AEGIS_SECRET_KEYS=Fernet.generate_key().decode(),AEGIS_AUTH_MODE='google',AEGIS_GOOGLE_CLIENT_ID='e2e-local-client',AEGIS_GOOGLE_CLIENT_SECRET='e2e-local-secret',AEGIS_GOOGLE_REDIRECT_URI=f'http://127.0.0.1:{args.port}/api/v1/auth/google/callback',AEGIS_INTEGRATION_MODE='mock',AEGIS_REQUIRE_RECONCILIATION='true',AEGIS_PLAN_EXECUTION_ENABLED='true',AEGIS_WRITEBACK_ENABLED='false',AEGIS_FILE_INBOX_ROOT=str(root/'inbox'))
    from aegis.core.config import reset_settings
    from aegis.core.db import dispose_engines,tenant_session
    from aegis.core.dbadmin import ensure_app_role,upgrade,grant_app_role
    from aegis.core.crypto import decrypt_json
    from aegis.core.tenancy import system_principal
    from aegis.core.util import utcnow,json_safe
    reset_settings();dispose_engines()
    engine=create_engine(url)
    with engine.begin() as c:ensure_app_role(c,'aegis_app',None)
    upgrade(url=args.database_url)
    with engine.begin() as c:grant_app_role(c,'aegis_app')
    from aegis.bootstrap import bootstrap_tenant
    from aegis.models import Tenant,AppUser,OAuthAttempt,InventoryReservation,ExecutionObservation,IngestionBatch,IntegrationConnection
    from aegis.auth import google
    tenant=bootstrap_tenant('e2e-'+secrets.token_hex(4),'AEGIS 试点工作空间','operator@e2e.test','e2e-password-unused-google')
    conn_id=seed(tenant,root)
    # Deliberate governance defects: independent export differs; an external SKU is unknown.
    from aegis.integration.reconciliation import reconcile
    from aegis.integration.raw import land_batch
    from aegis.integration.spi import RawItem
    from aegis.integration.normalize import normalize_batch
    with tenant_session(tenant) as s:
        source=json.loads((root/'governance-manifest.json').read_text())
        source['source_rows'][1]['payload']['on_hand']=8
        batch=s.scalar(select(IngestionBatch).where(IngestionBatch.connection_id==conn_id,IngestionBatch.stream=='inventory_snapshots'))
        reconcile(s,system_principal(tenant),batch.id,**source)
        land_batch(s,tenant_id=tenant,connection_id=conn_id,sync_run_id=None,items=[RawItem('daily_demand','external-fixture',{'sku':'EXT-CABLE','location':'W','date':utcnow().date().isoformat(),'units':2},utcnow())])
        normalize_batch(s,tenant)
    with tenant_session(tenant) as s:
        slug=s.get(Tenant,tenant).slug;user=s.scalar(select(AppUser));google.bind_identity(s,system_principal(tenant),user.id,'e2e-google-subject')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);nums=key.public_key().public_numbers()
    def b64(b):return base64.urlsafe_b64encode(b).decode().rstrip('=')
    keys={'keys':[{'kid':'e2e','kty':'RSA','alg':'RS256','n':b64(nums.n.to_bytes(256,'big')),'e':b64(nums.e.to_bytes(3,'big'))}]}
    def handler(request):
        if str(request.url)==google.JWKS_URL:return httpx.Response(200,json=keys)
        form=parse_qs(request.content.decode());attempt_id=uuid.UUID(form['code'][0])
        with tenant_session(tenant) as s:secret=decrypt_json(s.get(OAuthAttempt,attempt_id).secret_ciphertext)
        if form['code_verifier'][0]!=secret['verifier']:return httpx.Response(400,json={'error':'PKCE'})
        payload={'iss':'https://accounts.google.com','aud':'e2e-local-client','sub':'e2e-google-subject','email':'operator@e2e.test','email_verified':True,'nonce':secret['nonce'],'iat':int(time.time()),'exp':int(time.time())+300}
        msg=b64(json.dumps({'alg':'RS256','kid':'e2e'}).encode())+'.'+b64(json.dumps(payload).encode())
        return httpx.Response(200,json={'id_token':msg+'.'+b64(key.sign(msg.encode(),padding.PKCS1v15(),hashes.SHA256()))})
    google._client=lambda:httpx.Client(transport=httpx.MockTransport(handler))
    google.AUTH_URL=f'http://127.0.0.1:{args.port}/__e2e/google'
    from aegis.api.app import create_app
    from fastapi import Request
    from fastapi.responses import RedirectResponse
    app=create_app()
    @app.get('/__e2e/google')
    def local_google(state:str):
        _,attempt=google.state_ids(state)
        return RedirectResponse('/api/v1/auth/google/callback?state='+state+'&code='+str(attempt))
    @app.get('/__e2e/stock-fixtures')
    def stock_fixtures():
        # Synthetic external stock export, kept outside production routes and image.
        with tenant_session(tenant) as s:
            r=s.scalar(select(InventoryReservation).order_by(InventoryReservation.created_at.desc()).limit(1))
            e=s.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id==r.id).order_by(ExecutionObservation.source_version.desc()).limit(1))
            now=utcnow().isoformat();path=root/'inventory_snapshots.csv'
            records=[{'sku':'S','location':'GLOBAL-POOL','as_of':now,'on_hand':1000-e.shipped,'allocated':20,'in_transit':0,'backorder':0},
                {'sku':'S','location':'W','as_of':now,'on_hand':1+e.received,'allocated':0,'in_transit':0,'backorder':0}]
            with path.open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
            return {'file':str(path)}
    @app.get('/__e2e/source-manifest')
    def source_manifest():
        from aegis.integration.connections import build_ctx
        from aegis.integration.connectors.file_csv import FileCsvConnector
        from aegis.integration.spi import RawItem
        with tenant_session(tenant) as s:
            batch=s.scalar(select(IngestionBatch).where(IngestionBatch.connection_id==conn_id,IngestionBatch.stream=='inventory_snapshots').order_by(IngestionBatch.created_at.desc()).limit(1))
            e=s.scalar(select(ExecutionObservation).order_by(ExecutionObservation.created_at.desc()).limit(1))
            records=[r for r in FileCsvConnector().read(build_ctx(s.get(IntegrationConnection,conn_id)),'inventory_snapshots',None,None) if isinstance(r,RawItem)]
            data={'source_rows':[dict(external_id=r.external_id,payload=r.payload,source_updated_at=r.source_updated_at) for r in records],'scope':batch.scope,'evidence':'Independent synthetic CSV export and transfer proof','execution_refs':{e.external_ref:{'shipped':e.shipped,'received':e.received,'cancelled':e.cancelled}}}
            path=root/'source-manifest.json';path.write_text(json.dumps(json_safe(data)));return {'file':str(path),'batch_id':str(batch.id)}
    args.state.write_text(json.dumps({'tenant_id':str(tenant),'slug':slug,'connection_id':str(conn_id),'url':f'http://127.0.0.1:{args.port}','fixture_root':str(root)},indent=2))
    stop=threading.Event()
    def worker():
        from aegis.integration.runtime import run_sync_once
        from aegis.planning.lifecycle import run_plan_once
        while not stop.is_set():
            try:
                run_sync_once();run_plan_once()
                from aegis.integration.runtime import normalize_tenant
                normalize_tenant(tenant,max_batches=5)
            except Exception:
                import traceback;traceback.print_exc()
            stop.wait(.3)
    threading.Thread(target=worker,daemon=True).start()
    import uvicorn
    try:uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)
    finally:stop.set();engine.dispose()


if __name__=='__main__':main()
