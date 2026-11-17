from __future__ import annotations

import argparse
import getpass
import json
import os
import uuid
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aegis Predict operator CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db").add_subparsers(dest="action", required=True)
    db.add_parser("upgrade")
    down = db.add_parser("downgrade"); down.add_argument("revision")
    grant = db.add_parser("grant"); grant.add_argument("--role", default="aegis_app")
    create = db.add_parser("create-role"); create.add_argument("--role", default="aegis_app")
    tenant = commands.add_parser("tenant-create")
    tenant.add_argument("--slug", required=True); tenant.add_argument("--name", required=True); tenant.add_argument("--email", required=True)
    sample = commands.add_parser("import-sample")
    sample.add_argument("--tenant", type=uuid.UUID, required=True); sample.add_argument("--path", type=Path, default=Path("data/sample_network.json"))
    bind = commands.add_parser("identity-bind")
    bind.add_argument("--tenant", type=uuid.UUID, required=True)
    bind.add_argument("--email", required=True)
    bind.add_argument("--google-sub", required=True)
    worker = commands.add_parser("worker"); worker.add_argument("kind", choices=("sync", "plan", "forecast", "scheduler")); worker.add_argument("--once", action="store_true")
    serve = commands.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8000)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--path", type=Path, default=Path("data/sample_network.json")); backtest.add_argument("--output", type=Path)
    backtest.add_argument("--max-wape", type=float); backtest.add_argument("--max-bias", type=float)
    backtest.add_argument('--horizon',type=int,default=7)
    backtest.add_argument('--min-train',type=int,default=28)
    backtest.add_argument('--max-folds',type=int,default=8,help='last N disjoint origins per series; 0 means all')
    backtest.add_argument('--model-version',default='rolling-champion-v3',choices=['rolling-champion-v3','seasonal-tsb-v2','market-aware-v4'])
    backtest.add_argument('--max-baseline-regression',type=float,help='maximum fractional increase in paired absolute error vs mean28')
    check = commands.add_parser("pilot-check")
    check.add_argument("--tenant", type=uuid.UUID, required=True)
    release = commands.add_parser("release-manifest")
    release.add_argument("--root", type=Path, default=Path("."))
    release.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "db":
        from .core import dbadmin
        from .core.db import get_admin_engine
        if args.action == "upgrade":
            dbadmin.upgrade()
        elif args.action == "downgrade":
            dbadmin.downgrade(args.revision)
        else:
            with get_admin_engine().begin() as connection:
                if args.action == "create-role":
                    password = os.environ.get("AEGIS_APP_ROLE_PASSWORD") or getpass.getpass("Application DB password: ")
                    dbadmin.ensure_app_role(connection, args.role, password)
                else:
                    dbadmin.grant_app_role(connection, args.role)
    elif args.command == "tenant-create":
        from .bootstrap import bootstrap_tenant
        password = os.environ.get("AEGIS_BOOTSTRAP_PASSWORD") or getpass.getpass("Tenant admin password: ")
        print(bootstrap_tenant(args.slug, args.name, args.email, password))
    elif args.command == "identity-bind":
        from sqlalchemy import select
        from .models import AppUser
        from .core.db import tenant_session
        from .core.tenancy import system_principal
        from .auth.google import bind_identity
        with tenant_session(args.tenant) as session:
            user = session.scalar(select(AppUser).where(AppUser.email == args.email.strip().lower()))
            if user is None:
                raise SystemExit("workspace member not found")
            row = bind_identity(session, system_principal(args.tenant, "operator-cli"), user.id, args.google_sub)
            print(row.id)
    elif args.command == "import-sample":
        from .bootstrap import import_sample
        print(json.dumps(import_sample(args.tenant, args.path)))
    elif args.command == "worker":
        from .workers.runner import run
        run(args.kind, once=args.once)
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("aegis.api.app:app", host=args.host, port=args.port, proxy_headers=False, access_log=False)
    elif args.command == "pilot-check":
        from .core.db import tenant_session
        from .readiness import pilot_report
        with tenant_session(args.tenant) as session:
            result = pilot_report(session, args.tenant)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["data_ready"]:
            raise SystemExit(2)
    elif args.command == "release-manifest":
        from .release import manifest
        args.output.write_text(json.dumps(manifest(args.root.resolve()), ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.command == "backtest":
        from overseas_inventory.models import load_network
        from .planning.backtest import rolling_backtest
        result = rolling_backtest(load_network(args.path),horizon=args.horizon,min_train=args.min_train,
                                  max_folds_per_series=args.max_folds or None,model_version=args.model_version)
        if args.output:
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"model_version":result["model_version"],"horizon":result["horizon"],
            "models":{k:{"metrics":v["overall"],"vs_mean28":v["vs_baseline"]} for k,v in result["models"].items()}}, indent=2))
        overall = result["metrics"].get("overall", {})
        if not overall['folds']:raise SystemExit('no valid holdouts available')
        if args.max_baseline_regression is not None:
            if args.max_baseline_regression<0:raise SystemExit('max-baseline-regression must be nonnegative')
            paired=result['models'][result['primary_model']]['vs_baseline']
            if paired['absolute_error_difference']>args.max_baseline_regression*paired['baseline_error']+1e-5:
                raise SystemExit('paired mean28 regression threshold exceeded')
        if args.max_wape is not None and (overall["wape"] is None or overall["wape"] > args.max_wape):
            raise SystemExit("WAPE regression threshold exceeded")
        if args.max_bias is not None and (overall["bias"] is None or abs(overall["bias"]) > args.max_bias):
            raise SystemExit("Bias regression threshold exceeded")


if __name__ == "__main__":
    main()
