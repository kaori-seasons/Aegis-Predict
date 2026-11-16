"""Run a disposable PostgreSQL + non-root, read-only API smoke test. Never uses .env."""
from __future__ import annotations
import argparse
import json
import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--image',required=True);p.add_argument('--platform',required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    suffix=secrets.token_hex(5);network='aegis-verify-'+suffix;pg=network+'-pg';api=network+'-api'
    env=os.environ.copy()
    env.update(POSTGRES_PASSWORD=secrets.token_urlsafe(24),AEGIS_APP_ROLE_PASSWORD=secrets.token_urlsafe(24))
    from cryptography.fernet import Fernet
    env['AEGIS_SECRET_KEYS']=Fernet.generate_key().decode()
    owner=f"postgresql+psycopg://aegis_owner:{env['POSTGRES_PASSWORD']}@{pg}:5432/aegis"
    app=f"postgresql+psycopg://aegis_app:{env['AEGIS_APP_ROLE_PASSWORD']}@{pg}:5432/aegis"
    env.update(AEGIS_ADMIN_DATABASE_URL=owner,AEGIS_DATABASE_URL=app,AEGIS_ENV='prod',AEGIS_INTEGRATION_MODE='mock',AEGIS_AUTH_MODE='google')
    def docker(*cmd,check=True):
        r=subprocess.run(['docker',*cmd],env=env,text=True,capture_output=True)
        if check and r.returncode:
            # Avoid dumping environment-bearing inspect/config outputs on failure.
            raise RuntimeError('docker '+cmd[0]+' failed: '+r.stderr[-3000:])
        return r.stdout.strip()
    common=['--platform',args.platform,'--network',network,'--read-only','--tmpfs','/tmp:size=128m,mode=1777']
    def env_args(keys):return [v for key in keys for v in ('--env',key)]
    keys=['AEGIS_ENV','AEGIS_SECRET_KEYS','AEGIS_DATABASE_URL','AEGIS_INTEGRATION_MODE','AEGIS_AUTH_MODE']
    result={'image':args.image,'platform':args.platform,'synthetic_only':True}
    try:
        docker('network','create',network)
        docker('run','-d','--name',pg,'--network',network,'--env','POSTGRES_PASSWORD','--env','POSTGRES_USER=aegis_owner','--env','POSTGRES_DB=aegis','postgres:16-alpine')
        for _ in range(60):
            r=subprocess.run(['docker','exec',pg,'pg_isready','-U','aegis_owner','-d','aegis'],capture_output=True)
            if r.returncode==0:break
            time.sleep(.5)
        else:raise RuntimeError('PostgreSQL readiness timeout')
        migration_keys=keys+['AEGIS_ADMIN_DATABASE_URL','AEGIS_APP_ROLE_PASSWORD']
        docker('run','--rm',*common,*env_args(migration_keys),args.image,'sh','-ec','aegis db create-role && aegis db upgrade && aegis db grant')
        docker('run','-d','--name',api,*common,*env_args(keys),'-p','127.0.0.1::8000',args.image)
        port=docker('port',api,'8000/tcp').rsplit(':',1)[1]
        base='http://127.0.0.1:'+port
        for _ in range(60):
            try:
                with urllib.request.urlopen(base+'/health/ready',timeout=2) as r:
                    if r.status==200:break
            except Exception:time.sleep(.5)
        else:raise RuntimeError('API readiness timeout')
        def get(path):
            with urllib.request.urlopen(base+path,timeout=5) as r:return json.load(r)
        assert get('/health/live')['status']=='ok'
        with urllib.request.urlopen(base+'/',timeout=5) as r: html=r.read().decode()
        assert 'workspace-assets/workspace.js?v=' in html and '__JS_HASH__' not in html
        with urllib.request.urlopen(base+'/workspace-assets/workspace.js',timeout=5) as r: js=r.read().decode()
        assert '/api/v1/workspace/overview' in js and '/student/' not in js
        with urllib.request.urlopen(base+'/workspace-assets/administration.js',timeout=5) as r: admin_js=r.read().decode()
        assert '/api/v1/users' in admin_js and 'administration.js?v=' in html and '__ADMIN_HASH__' not in html
        with urllib.request.urlopen(base+'/login',timeout=5) as r: assert 'google' in r.read().decode().lower()
        providers=get('/api/v1/auth/providers')
        assert providers['default']=='google' and providers['platform_data_mode']=='mock'
        assert docker('exec',api,'id','-u')=='10001'
        assert docker('exec',api,'python','-c',"from pathlib import Path; assert Path('/app/artifacts/runtime-wheel-sha256.txt').read_text(); from aegis.core.dbadmin import head_revision; print(head_revision())")=='0005_governance'
        runtime=json.loads(docker('exec',api,'python','-c',"import json; from aegis.release import manifest; from pathlib import Path; print(json.dumps(manifest(Path('/app'))))"))
        docker('run','--rm',*common,*env_args(keys),args.image,'aegis','backtest','--max-wape','0.35','--max-bias','0.2')
        result.update(backtest_passed=True,migration='0005_governance',health_ready=True,health_live=True,uid=10001,read_only_root=True,
            source_tree_digest=runtime['source_tree_digest'],auth_default=providers['default'],platform_data_mode='mock',
            image_id=json.loads(docker('image','inspect',args.image,'--format','{{json .Id}}')),
            wheel_hashes_present=True,workspace_assets=True)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
    finally:
        docker('rm','-f',api,pg,check=False)
        docker('network','rm',network,check=False)


if __name__=='__main__':main()
