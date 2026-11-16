from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import hashlib
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..core import security
from ..core.audit import record_audit
from ..core.config import get_settings
from ..core.db import get_engine, global_session, tenant_session
from ..core.errors import AegisError, InvalidInput, NotFound, PermissionDenied, Unauthenticated
from ..core.tenancy import new_trace_id, reset_trace_id, set_trace_id
from ..core.util import json_safe
from ..integration import connections
from ..integration.mapping import resolve_mapping
from ..integration.normalize import replay_quarantine
from ..integration.registry import available_kinds
from ..models import (AuditLog, IdMapping, IntegrationConnection, PlanLine, PlanLinePin, PlanRun,
                      PolicyVersion, QuarantineRecord, SyncRun, Tenant, WritebackCommand)
from ..planning import lifecycle
from ..planning.policy import PlanningPolicy


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Login(Body):
    tenant: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class ConnectionBody(Body):
    name: str = Field(min_length=1, max_length=160)
    kind: str
    config: dict[str, Any]
    secrets: dict[str, Any] = Field(default_factory=dict)
    source_system: str | None = Field(None, max_length=80)
    schedule_interval_seconds: int | None = Field(None, ge=60)


class ConnectionUpdateBody(Body):
    config: dict[str, Any] | None = None
    status: str | None = None
    schedule_interval_seconds: int | None = Field(None, ge=60)
    reconcile_interval_seconds: int | None = Field(None, ge=60)


class SecretsBody(Body):
    secrets: dict[str, Any]


class SyncBody(Body):
    mode: str = "incremental"
    streams: list[str] | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


class PlanBody(Body):
    horizon_days: int = Field(30, ge=1, le=365)
    seed: int = Field(42, ge=0, le=2**31-1)
    cutoff_at: datetime | None = None
    writeback_connection_id: uuid.UUID | None = None
    mode: str = "shadow"


class AdjustBody(Body):
    units: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=2000)
    pin: bool = True


class DecisionBody(Body):
    approve: bool
    note: str = Field("", max_length=2000)


class ReleaseBody(Body):
    note: str = Field("", max_length=2000)
    acknowledge_stale: bool = False
    command_type: str = "create_transfer"


class ActionBody(Body):
    note: str = Field(min_length=1, max_length=2000)


class MappingBody(Body):
    target_code: str = Field(min_length=1, max_length=80)
    expected_updated_at: datetime | None = None
    note: str = Field("", max_length=2000)


class AuthorityBody(Body):
    domain: str
    location_id: uuid.UUID
    connection_id: uuid.UUID
    stream: str
    channel: str = Field("*", max_length=80)
    inbound_mode: str = "linked"
    coverage_start: date | None = None
    note: str = Field(min_length=1, max_length=2000)


class InboundLinkBody(Body):
    snapshot_id: uuid.UUID
    purchase_order_line_id: uuid.UUID
    units: int = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=2000)


class GoogleStartBody(Body):
    tenant: str = Field(min_length=1, max_length=64)


class GoogleBindingBody(Body):
    subject: str = Field(min_length=1, max_length=255)


class ReconciliationBody(Body):
    source_rows: list[dict] = Field(max_length=50000)
    scope: dict
    evidence: str = Field(min_length=1, max_length=2000)
    execution_refs: dict = Field(default_factory=dict)


class ExecutionBody(Body):
    connection_id: uuid.UUID
    source_version: int = Field(ge=1, le=2**63-1)
    external_ref: str = Field(min_length=1, max_length=200)
    shipped: int = Field(ge=0, le=2**31-1)
    received: int = Field(ge=0, le=2**31-1)
    cancelled: int = Field(ge=0, le=2**31-1)
    is_final: bool = False
    occurred_at: datetime
    evidence: str = Field(min_length=1, max_length=2000)


class SettlementBody(Body):
    source_batch_id: uuid.UUID
    destination_batch_id: uuid.UUID | None = None
    evidence: str = Field(min_length=1, max_length=2000)


class ReplayBody(Body):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    accept_quality: bool = False
    note: str = Field("", max_length=2000)


class UserBody(Body):
    email: str = Field(min_length=1, max_length=320)
    password: str | None = Field(None, min_length=12, max_length=1024)
    roles: list[str] = Field(min_length=1, max_length=5)
    location_scope: list[str] | None = None
    display_name: str = Field("", max_length=200)
    google_subject: str | None = Field(None, min_length=1, max_length=255)


class UserUpdateBody(Body):
    expected_updated_at: datetime
    note: str = Field(min_length=1, max_length=2000)
    roles: list[str] | None = Field(None, min_length=1, max_length=5)
    location_scope: list[str] | None = None
    is_active: bool | None = None
    display_name: str | None = Field(None, max_length=200)


class DispositionBody(Body):
    status: str = Field(pattern="^(open|in_progress|resolved)$")
    expected_revision: int = Field(ge=0)
    assignee_id: uuid.UUID | None = None
    resolution_check_id: uuid.UUID | None = None
    note: str = Field(min_length=1, max_length=2000)


def view(row, *, exclude: set[str] | None = None) -> dict:
    omitted = (exclude or set()) | {"password_hash", "secret_ciphertext", "token_hash"}
    return json_safe({a.key: getattr(row, a.key) for a in inspect(row).mapper.column_attrs if a.key not in omitted})


def context(request: Request):
    settings = get_settings()
    header = request.headers.get("authorization", "")
    method = "bearer" if header.startswith("Bearer ") else "cookie"
    token = header[7:] if method == "bearer" else request.cookies.get(settings.session_cookie_name)
    if not token:
        raise Unauthenticated("login required")
    if method == "cookie" and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not security.csrf_matches(request.cookies.get(settings.csrf_cookie_name), request.headers.get("x-csrf-token")):
            raise PermissionDenied("CSRF token missing or invalid")
    with global_session() as session:
        issued = security.lookup_session(session, token)
    # FastAPI may enter/exit sync yield dependencies in different copied thread contexts.
    # Tenant isolation uses SET LOCAL, never the optional logging ContextVar.
    with tenant_session(issued.tenant_id, bind_context=False) as session:
        principal = security.load_principal(session, issued, method)
        tenant = session.get(Tenant, principal.tenant_id)
        if tenant is None or tenant.status != "active":
            raise Unauthenticated("tenant is disabled")
        yield session, principal


def allowed(permission: str):
    def dependency(ctx=Depends(context, scope="function")):
        session, principal = ctx
        if not principal.can(permission):
            raise PermissionDenied(f"permission required: {permission}")
        lifecycle.require_global_scope(principal)
        return session, principal
    return dependency


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    # Fail startup if the runtime role can bypass the tenant boundary.
    with get_engine().connect() as connection:
        from ..core.dbadmin import current_revision, head_revision
        if current_revision(connection) != head_revision():
            raise RuntimeError("database migrations are not current")
        unsafe = connection.execute(text("""
            SELECT rolsuper OR rolbypassrls OR EXISTS (
                SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner=current_user
            ) FROM pg_roles WHERE rolname=current_user
        """)).scalar_one()
        if unsafe:
            raise RuntimeError("application database role must be a non-owner without SUPERUSER/BYPASSRLS")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Aegis Predict", version="0.9.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().allowed_hosts)

    @app.middleware("http")
    async def trace(request, call_next):
        trace_id = new_trace_id()
        token = set_trace_id(trace_id)
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            reset_trace_id(token)

    @app.exception_handler(AegisError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": json_safe(exc.details)}})

    @app.exception_handler(ValidationError)
    async def policy_error(request, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "message": "invalid policy or connector configuration"}})

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        issues = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "issues": issues}})

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(status_code=409, content={"error": {"code": "conflict", "message": "conflicting or invalid reference"}})

    @app.exception_handler(OperationalError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"error": {"code": "database_unavailable", "message": "retry the request"}})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.mount("/workspace-assets", StaticFiles(directory=Path(__file__).parent / "static"), name="workspace-assets")

    @app.get("/", response_class=HTMLResponse)
    def workspace_entry():
        root = Path(__file__).parent
        html = (root / "workspace.html").read_text()
        for suffix in ("css", "js", "admin"):
            filename = "administration.js" if suffix=="admin" else "workspace."+suffix
            digest = hashlib.sha256((root / "static" / filename).read_bytes()).hexdigest()[:12]
            html = html.replace("__"+suffix.upper()+"_HASH__", digest)
        return html

    @app.get("/login", response_class=HTMLResponse)
    def auth_entry():
        return (Path(__file__).parent / "auth.html").read_text()

    @app.get("/api/v1/workspace/overview")
    def workspace_overview(ctx=Depends(allowed(security.DATA_VIEW))):
        from .workspace import overview
        return overview(*ctx)

    @app.get("/api/v1/workspace/catalog")
    def workspace_catalog(limit: int=Query(500,ge=1,le=500), offset: int=Query(0,ge=0),ctx=Depends(allowed(security.DATA_VIEW))):
        from .workspace import catalog
        return catalog(ctx[0],limit=limit,offset=offset)

    @app.get("/api/v1/workspace/plans/{plan_id}/forecast")
    def workspace_forecast(plan_id: uuid.UUID,line_id: uuid.UUID,horizon: int=Query(30,ge=1,le=365),ctx=Depends(allowed(security.PLAN_VIEW))):
        from .workspace import plan_forecast
        return json_safe(plan_forecast(ctx[0],plan_id,line_id,horizon))

    @app.get("/api/v1/workspace/plans/{plan_id}/evaluation")
    def workspace_evaluation(plan_id: uuid.UUID,horizon: int | None=Query(None,ge=1,le=365),ctx=Depends(allowed(security.PLAN_VIEW))):
        from .workspace import evaluation
        return json_safe(evaluation(ctx[0],plan_id,horizon=horizon))

    @app.get("/api/v1/workspace/plans/{plan_id}/execution")
    def workspace_execution(plan_id: uuid.UUID,ctx=Depends(allowed(security.PLAN_VIEW))):
        from .workspace import execution
        return execution(ctx[0],plan_id)

    @app.get("/api/v1/auth/providers")
    def providers():
        settings = get_settings()
        return {"default": settings.auth_mode, "google_configured": bool(settings.google_client_id and settings.google_client_secret.get_secret_value() and settings.google_redirect_uri),
                "platform_data_mode": settings.integration_mode,"csrf_cookie_name": settings.csrf_cookie_name,
                "execution_enabled": settings.plan_execution_enabled,"writeback_enabled": settings.writeback_enabled}

    @app.post("/api/v1/auth/google/start")
    def google_start(body: GoogleStartBody, request: Request, response: Response):
        from ..auth.google import begin, CALLBACK_PATH
        if request.headers.get("sec-fetch-site") == "cross-site" or get_settings().auth_mode == "password":
            raise PermissionDenied("Google login unavailable for this request")
        flow = begin(body.tenant)
        response.set_cookie(flow["cookie_name"], flow["browser"], max_age=600, httponly=True,
                            secure=get_settings().cookie_secure, samesite="lax", path=CALLBACK_PATH)
        return {"authorization_url": flow["authorization_url"]}

    @app.get("/api/v1/auth/google/callback")
    def google_callback(request: Request, state: str = Query(max_length=256), code: str = Query("", max_length=4096)):
        from ..auth.google import complete, state_ids, CALLBACK_PATH
        _, attempt_id = state_ids(state)
        cookie_name = "aegis_oidc_"+attempt_id.hex
        issued = complete(state, code, request.cookies.get(cookie_name, ""),
            ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))
        settings = get_settings()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(settings.session_cookie_name, issued.token, max_age=issued.expires_in_seconds,
                            httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        response.set_cookie(settings.csrf_cookie_name, security.new_csrf_token(), max_age=issued.expires_in_seconds,
                            httponly=False, secure=settings.cookie_secure, samesite="strict", path="/")
        response.delete_cookie(cookie_name, path=CALLBACK_PATH)
        return response

    @app.post("/api/v1/users/{user_id}/google-identity", status_code=201)
    def google_binding(user_id: uuid.UUID, body: GoogleBindingBody, ctx=Depends(allowed(security.USER_MANAGE))):
        from ..auth.google import bind_identity
        session, principal = ctx
        return view(bind_identity(session, principal, user_id, body.subject))

    @app.post("/api/v1/auth/login")
    def login(body: Login, request: Request, response: Response):
        if get_settings().auth_mode == "google":
            raise PermissionDenied("password login is disabled; use Google sign-in")
        # Login is a simple JSON POST; browsers cannot cross-origin it without a preflight,
        # and the service does not enable CORS. Sec-Fetch-Site blocks same-site login CSRF.
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise PermissionDenied("cross-site login rejected")
        with global_session() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.slug == body.tenant))
        issued = None
        if tenant:
            with tenant_session(tenant.id) as session:
                issued = security.authenticate(session, tenant, body.email, body.password,
                    ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))
        else:
            security.verify_password(security._DUMMY_HASH, body.password)
        if issued is None:
            raise Unauthenticated("invalid credentials")
        settings = get_settings()
        csrf = security.new_csrf_token()
        response.set_cookie(settings.session_cookie_name, issued.token, max_age=issued.expires_in_seconds,
                            httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        response.set_cookie(settings.csrf_cookie_name, csrf, max_age=issued.expires_in_seconds,
                            httponly=False, secure=settings.cookie_secure, samesite="strict", path="/")
        return {"tenant_id": str(issued.tenant_id), "csrf_token": csrf, "expires_in": issued.expires_in_seconds}

    @app.get("/api/v1/auth/me")
    def me(ctx=Depends(context, scope="function")):
        _, p = ctx
        return {"user_id": str(p.user_id), "tenant_id": str(p.tenant_id), "email": p.label, "roles": sorted(p.roles), "permissions": sorted(p.permissions)}

    @app.post("/api/v1/auth/logout")
    def logout(response: Response, ctx=Depends(context, scope="function")):
        session, p = ctx
        security.revoke_session(session, p.session_id)
        response.delete_cookie(get_settings().session_cookie_name)
        response.delete_cookie(get_settings().csrf_cookie_name)
        return {"status": "logged_out"}

    @app.post("/api/v1/users", status_code=201)
    def create_user(body: UserBody, ctx=Depends(allowed(security.USER_MANAGE))):
        from .administration import create_member
        session, p = ctx
        return view(create_member(session,p,**body.model_dump()))

    @app.get('/api/v1/users')
    def list_users(q: str=Query('',max_length=160),limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0),ctx=Depends(allowed(security.USER_MANAGE))):
        from .administration import members
        return members(ctx[0],q=q,limit=limit,offset=offset)

    @app.get('/api/v1/roles')
    def roles(ctx=Depends(allowed(security.USER_MANAGE))):
        return {key:sorted(value) for key,value in security.ROLE_PERMISSIONS.items()}

    @app.patch('/api/v1/users/{user_id}')
    def update_user(user_id: uuid.UUID,body: UserUpdateBody,ctx=Depends(allowed(security.USER_MANAGE))):
        from .administration import update_member
        data=body.model_dump(exclude_unset=True)
        if any(data.get(k) is None for k in ('roles','is_active','display_name') if k in data):
            raise InvalidInput('member fields cannot be null')
        return view(update_member(*ctx,user_id,**data))

    @app.post('/api/v1/users/{user_id}/revoke-sessions')
    def revoke_user(user_id: uuid.UUID,body: ActionBody,ctx=Depends(allowed(security.USER_MANAGE))):
        from ..models import AppUser
        session,p=ctx
        user=session.get(AppUser,user_id)
        if user is None:raise NotFound('member not found')
        security.revoke_user_sessions(session,user.id)
        record_audit(session,p,'user.sessions.revoke','app_user',user.id,after={'note':body.note})
        return {'status':'revoked'}

    @app.get('/api/v1/workspace/reconciliation-assignees')
    def eligible_assignees(ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..models import AppUser
        return [{'id':str(u.id),'email':u.email} for u in ctx[0].scalars(select(AppUser).where(AppUser.is_active,AppUser.location_scope.is_(None)).order_by(AppUser.email))
                if security.CONNECTION_EDIT in security.permissions_for(u.roles)]

    @app.get('/api/v1/workspace/mappings')
    def workspace_mappings(q: str=Query('',max_length=160),status: str|None=Query(None,pattern='^(pending|confirmed|auto)$'),entity: str|None=Query(None,pattern='^(sku|location)$'),limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0),ctx=Depends(allowed(security.CONNECTION_VIEW))):
        from .administration import mapping_rows
        return mapping_rows(ctx[0],q=q,status=status,entity=entity,limit=limit,offset=offset)

    @app.get('/api/v1/workspace/catalog/search')
    def catalog_search(entity: str=Query(pattern='^(sku|location)$'),q: str=Query('',max_length=160),ctx=Depends(allowed(security.CONNECTION_VIEW))):
        from ..models import Sku,Location
        model=Sku if entity=='sku' else Location
        rows=ctx[0].scalars(select(model).where(model.is_active,model.code.icontains(q,autoescape=True)).order_by(model.code).limit(50))
        return [{'code':r.code,'name':r.name} for r in rows]

    @app.get('/api/v1/workspace/reconciliations')
    def workspace_checks(q: str=Query('',max_length=160),status: str|None=Query(None,pattern='^(matched|mismatch)$'),limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0),ctx=Depends(allowed(security.DATA_VIEW))):
        from .administration import reconciliation_rows
        return reconciliation_rows(ctx[0],q=q,status=status,limit=limit,offset=offset)

    @app.get('/api/v1/data/reconciliations/{check_id}')
    def check_detail(check_id: uuid.UUID,ctx=Depends(allowed(security.DATA_VIEW))):
        from .administration import reconciliation_detail
        return reconciliation_detail(ctx[0],check_id)

    @app.post('/api/v1/data/batches/{batch_id}/disposition')
    def check_disposition(batch_id: uuid.UUID,body: DispositionBody,ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from .administration import disposition
        return view(disposition(*ctx,batch_id,**body.model_dump()))

    @app.get("/api/v1/integration/kinds")
    def kinds(ctx=Depends(allowed(security.CONNECTION_VIEW))):
        return {"kinds": available_kinds()}

    @app.get("/api/v1/integration/markets")
    def markets(ctx=Depends(allowed(security.CONNECTION_VIEW))):
        from ..integration.markets import catalogue
        return catalogue()

    @app.get("/api/v1/data/authorities")
    def source_authorities(ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import SourceAuthority
        session, _ = ctx
        return [view(row) for row in session.scalars(select(SourceAuthority).order_by(SourceAuthority.created_at))]

    @app.get("/api/v1/data/pilot-readiness")
    def pilot_readiness(ctx=Depends(allowed(security.DATA_VIEW))):
        from ..readiness import pilot_report
        session, p = ctx
        return pilot_report(session, p.tenant_id)

    @app.delete("/api/v1/data/authorities/{authority_id}")
    def remove_authority(authority_id: uuid.UUID, body: ActionBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..models import SourceAuthority
        session, p = ctx
        row = session.get(SourceAuthority, authority_id, with_for_update=True)
        if row is None:
            raise NotFound("source authority not found")
        record_audit(session, p, "data.authority.remove", "source_authority", row.id,
                     before=view(row), after={"note": body.note})
        session.delete(row)

    @app.post("/api/v1/data/authorities")
    def authority(body: AuthorityBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..integration.authority import set_authority
        session, p = ctx
        return view(set_authority(session, p, **body.model_dump()))

    @app.get("/api/v1/data/batches")
    def batches(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import IngestionBatch
        session, _ = ctx
        return [view(row) for row in session.scalars(select(IngestionBatch).order_by(IngestionBatch.created_at.desc()).limit(limit))]

    @app.get("/api/v1/inventory/snapshots")
    def snapshots(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import InventorySnapshot
        session, _ = ctx
        return [view(row) for row in session.scalars(select(InventorySnapshot).order_by(InventorySnapshot.as_of.desc()).limit(limit))]

    @app.get("/api/v1/inventory/purchase-orders")
    def purchase_orders(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import PurchaseOrderLine
        session, _ = ctx
        return [view(row) for row in session.scalars(select(PurchaseOrderLine).order_by(PurchaseOrderLine.created_at.desc()).limit(limit))]

    @app.post("/api/v1/inventory/inbound-links")
    def inbound_links(body: InboundLinkBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..inventory.service import link_inbound
        session, p = ctx
        return view(link_inbound(session, p, **body.model_dump()))

    @app.get("/api/v1/inventory/reservations")
    def reservations(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import InventoryReservation
        session, _ = ctx
        return [view(row) for row in session.scalars(select(InventoryReservation).order_by(InventoryReservation.created_at.desc()).limit(limit))]

    @app.post("/api/v1/data/batches/{batch_id}/reconcile", status_code=201)
    def source_reconcile(batch_id: uuid.UUID, body: ReconciliationBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..integration.reconciliation import reconcile
        session, principal = ctx
        return view(reconcile(session, principal, batch_id, **body.model_dump()))

    @app.get("/api/v1/data/reconciliations")
    def reconciliation_history(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import SourceReconciliation
        session, _ = ctx
        return [view(row) for row in session.scalars(select(SourceReconciliation).order_by(SourceReconciliation.created_at.desc()).limit(limit))]

    @app.post("/api/v1/inventory/reservations/{reservation_id}/execution", status_code=201)
    def physical_execution(reservation_id: uuid.UUID, body: ExecutionBody, ctx=Depends(allowed(security.PLAN_RELEASE))):
        from ..inventory.execution import observe
        session, principal = ctx
        return view(observe(session, principal, reservation_id, **body.model_dump()))

    @app.post("/api/v1/inventory/reservations/{reservation_id}/settle", status_code=201)
    def physical_settlement(reservation_id: uuid.UUID, body: SettlementBody, ctx=Depends(allowed(security.PLAN_RELEASE))):
        from ..inventory.execution import settle
        session, principal = ctx
        return view(settle(session, principal, reservation_id, **body.model_dump()))

    @app.get("/api/v1/inventory/execution-observations")
    def physical_history(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.DATA_VIEW))):
        from ..models import ExecutionObservation
        session, _ = ctx
        return [view(row) for row in session.scalars(select(ExecutionObservation).order_by(ExecutionObservation.created_at.desc()).limit(limit))]

    @app.get("/api/v1/data/batches/{batch_id}/mock-source-manifest")
    def mock_manifest(batch_id: uuid.UUID, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..models import IngestionBatch
        from ..integration.connectors.mock_platforms import manifest_for_batch
        session, _ = ctx
        batch = session.get(IngestionBatch, batch_id)
        if batch is None:
            raise NotFound("batch unavailable")
        return json_safe(manifest_for_batch(session, connections.get_connection(session, batch.connection_id), batch))

    @app.post("/api/v1/connections/{connection_id}/mock-authorize")
    def mock_authorize(connection_id: uuid.UUID, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from ..integration.connectors.mock_platforms import authorize
        session, principal = ctx
        conn = connections.get_connection(session, connection_id, lock=True)
        authorize(session, principal, conn)
        return connections.connection_view(conn)

    @app.get("/api/v1/connections")
    def list_connections(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.CONNECTION_VIEW))):
        session, _ = ctx
        return [connections.connection_view(c) for c in session.scalars(select(IntegrationConnection).order_by(IntegrationConnection.created_at.desc()).limit(limit))]

    @app.post("/api/v1/connections", status_code=201)
    def add_connection(body: ConnectionBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        session, p = ctx
        return connections.connection_view(connections.create_connection(session, p, **body.model_dump()))

    @app.patch("/api/v1/connections/{connection_id}")
    def connection_update(connection_id: uuid.UUID, body: ConnectionUpdateBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        session, principal = ctx
        row = connections.get_connection(session, connection_id, lock=True)
        return connections.connection_view(connections.update_connection(session, principal, row, **body.model_dump(exclude_unset=True)))

    @app.put("/api/v1/connections/{connection_id}/secrets")
    def secrets(connection_id: uuid.UUID, body: SecretsBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        session, p = ctx
        row = connections.get_connection(session, connection_id, lock=True)
        connections.set_secrets(session, p, row, body.secrets)
        return {"status": "updated"}

    @app.post("/api/v1/connections/{connection_id}/files/{stream}", status_code=201)
    def upload(connection_id: uuid.UUID, stream: str, file: UploadFile,
               ctx=Depends(allowed(security.CONNECTION_EDIT))):
        from pathlib import Path
        from ..integration.connectors.file_csv import FileCsvConnector
        from ..integration.spi import ConnCtx
        from ..integration.ratelimit import RateLimiter
        from ..integration.streams import STREAMS
        session, p = ctx
        row = connections.get_connection(session, connection_id, lock=True)
        if row.kind != "file_csv" or stream not in STREAMS:
            raise InvalidInput("CSV/XLSX uploads require a file_csv connection and a canonical stream")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".csv", ".xlsx"):
            raise InvalidInput("only CSV/XLSX uploads are accepted")
        connector = FileCsvConnector()
        directory = connector._directory(ConnCtx(p.tenant_id, row.id, row.source_system, row.config, {}, get_settings(), RateLimiter()))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{stream}-{uuid.uuid4().hex}{suffix}"
        temporary = target.with_suffix(".part")
        size = 0
        try:
            with temporary.open("xb") as handle:
                while chunk := file.file.read(1 << 20):
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise InvalidInput("file exceeds 25 MiB")
                    handle.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        record_audit(session, p, "integration.file.upload", "integration_connection", row.id,
                     after={"file": target.name, "stream": stream, "bytes": size})
        return {"filename": target.name, "bytes": size, "source_file_version": datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(), "next_action": "request a sync for this stream"}

    @app.get("/api/v1/integration/templates")
    def templates(ctx=Depends(allowed(security.CONNECTION_VIEW))):
        from ..integration.streams import STREAMS
        return {name: {"fields": spec.field_names, "required": spec.required_fields, "kind": spec.kind}
                for name, spec in STREAMS.items()}

    @app.post("/api/v1/connections/{connection_id}/sync", status_code=202)
    def sync(connection_id: uuid.UUID, body: SyncBody, ctx=Depends(allowed(security.SYNC_RUN))):
        session, p = ctx
        if body.mode == "backfill" and not p.can(security.BACKFILL_RUN):
            raise PermissionDenied("backfill permission required")
        if body.window_start and body.window_start.tzinfo is None or body.window_end and body.window_end.tzinfo is None:
            raise InvalidInput("sync window timestamps must include timezone")
        row = connections.get_connection(session, connection_id, lock=True)
        return view(connections.request_sync(session, p, row, **body.model_dump()))

    @app.post("/api/v1/connections/{connection_id}/resume")
    def resume(connection_id: uuid.UUID, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        session, p = ctx
        connections.resume_connection(session, p, connections.get_connection(session, connection_id, lock=True))
        return {"status": "active"}

    @app.post("/api/v1/connections/{connection_id}/streams/{stream}/reset")
    def stream_reset(connection_id: uuid.UUID, stream: str, body: ActionBody, ctx=Depends(allowed(security.CONNECTION_EDIT))):
        session, p = ctx
        connections.reset_stream(session, p, connections.get_connection(session, connection_id, lock=True), stream)
        return {"status": "reset", "note": body.note}

    @app.get("/api/v1/sync-runs")
    def runs(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.CONNECTION_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(limit))]

    @app.get("/api/v1/mappings")
    def mappings(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.CONNECTION_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(IdMapping).order_by(IdMapping.created_at.desc()).limit(limit))]

    @app.post("/api/v1/mappings/{mapping_id}/resolve")
    def mapping_resolve(mapping_id: uuid.UUID, body: MappingBody, ctx=Depends(allowed(security.MAPPING_RESOLVE))):
        session, p = ctx
        row = session.get(IdMapping, mapping_id, with_for_update=True)
        if row is None:
            raise NotFound("mapping not found")
        from ..core.errors import Conflict
        if body.expected_updated_at is not None and body.expected_updated_at!=row.updated_at:
            raise Conflict('mapping changed; refresh before editing')
        # Remapping historical facts requires a dedicated rebuild, not a cosmetic edit.
        if row.status!='pending':
            raise Conflict('confirmed mappings are immutable; correct master data through a reviewed rebuild')
        target = resolve_mapping(session, row, target_code=body.target_code, resolved_by=p.label)
        ids=list(session.scalars(select(QuarantineRecord.id).join(IntegrationConnection,IntegrationConnection.id==QuarantineRecord.connection_id)
            .where(IntegrationConnection.source_system==row.source_system,QuarantineRecord.status=='open',
                   QuarantineRecord.reason_code.in_(['unmapped_sku','unmapped_location'])).limit(500)))
        count = replay_quarantine(session, p.tenant_id, quarantine_ids=ids, resolved_by=p.label)
        record_audit(session, p, "integration.mapping.resolve", "id_mapping", row.id,
            after={"target": target, "requeued": count,"note":body.note})
        return {"target_id": str(target), "replayed": count, "requeued":count}

    @app.get("/api/v1/quarantine")
    def quarantine(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.CONNECTION_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(QuarantineRecord).where(QuarantineRecord.status == "open").order_by(QuarantineRecord.created_at.desc()).limit(limit))]

    @app.post("/api/v1/quarantine/replay")
    def replay(body: ReplayBody, ctx=Depends(allowed(security.MAPPING_RESOLVE))):
        session, p = ctx
        count = replay_quarantine(session, p.tenant_id, quarantine_ids=body.ids, resolved_by=p.label, accept_quality=body.accept_quality)
        record_audit(session, p, "integration.quarantine.replay", "quarantine_record", after={"ids": body.ids, "count": count, "accept_quality": body.accept_quality, "note": body.note})
        return {"replayed": count}

    @app.post("/api/v1/policies", status_code=201)
    def policy(body: PlanningPolicy, ctx=Depends(allowed(security.POLICY_EDIT))):
        session, p = ctx
        return view(lifecycle.create_policy(session, p, body.model_dump(mode="json")))

    @app.get("/api/v1/policies")
    def policies(ctx=Depends(allowed(security.PLAN_VIEW))):
        session, _ = ctx
        return [view(p) for p in session.scalars(select(PolicyVersion).order_by(PolicyVersion.created_at.desc()).limit(100))]

    @app.post("/api/v1/plans", status_code=202)
    def plan_create(body: PlanBody, ctx=Depends(allowed(security.PLAN_GENERATE))):
        session, p = ctx
        return view(lifecycle.request_plan(session, p, **body.model_dump()))

    @app.get("/api/v1/plans")
    def plans(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.PLAN_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(PlanRun).order_by(PlanRun.created_at.desc()).limit(limit))]

    @app.get("/api/v1/plans/{plan_id}")
    def plan_view(plan_id: uuid.UUID, ctx=Depends(allowed(security.PLAN_VIEW))):
        session, _ = ctx
        plan = lifecycle.get_plan(session, plan_id)
        return {**view(plan), "lines": [view(l) for l in session.scalars(select(PlanLine).where(PlanLine.plan_run_id == plan.id).order_by(PlanLine.sku_code, PlanLine.location_code))]}

    @app.post("/api/v1/plans/{plan_id}/reproduce")
    def reproduce(plan_id: uuid.UUID, ctx=Depends(allowed(security.PLAN_GENERATE))):
        session, _ = ctx
        return lifecycle.reproduce(session, lifecycle.get_plan(session, plan_id))

    @app.patch("/api/v1/plans/{plan_id}/lines/{line_id}")
    def adjust(plan_id: uuid.UUID, line_id: uuid.UUID, body: AdjustBody, ctx=Depends(allowed(security.PLAN_ADJUST))):
        session, p = ctx
        return view(lifecycle.adjust_line(session, p, plan_id, line_id, **body.model_dump()))

    @app.post("/api/v1/plans/{plan_id}/lines/{line_id}/decision")
    def decision(plan_id: uuid.UUID, line_id: uuid.UUID, body: DecisionBody, ctx=Depends(allowed(security.PLAN_APPROVE))):
        session, p = ctx
        return view(lifecycle.decide_line(session, p, plan_id, line_id, **body.model_dump()))

    @app.post("/api/v1/plans/{plan_id}/release")
    def release(plan_id: uuid.UUID, body: ReleaseBody, ctx=Depends(allowed(security.PLAN_RELEASE))):
        session, p = ctx
        return view(lifecycle.release_plan(session, p, plan_id, **body.model_dump()))

    @app.post("/api/v1/plans/{plan_id}/actions/{action}")
    def action(plan_id: uuid.UUID, action: str, body: ActionBody, ctx=Depends(context)):
        session, p = ctx
        lifecycle.require_global_scope(p)
        required=security.PLAN_RELEASE if action=="close" else security.PLAN_ADJUST
        if not p.can(required):
            raise PermissionDenied("permission required: "+required)
        return view(lifecycle.terminate_plan(session, p, plan_id, action=action, note=body.note))

    @app.delete("/api/v1/pins/{pin_id}")
    def unpin(pin_id: uuid.UUID, ctx=Depends(allowed(security.PLAN_ADJUST))):
        session, p = ctx
        row = session.get(PlanLinePin, pin_id, with_for_update=True)
        if row is None:
            raise NotFound("pin not found")
        row.is_active = False
        record_audit(session, p, "plan.unpin", "plan_line_pin", row.id, after={"is_active": False})
        return {"status": "unpinned"}

    @app.get("/api/v1/writebacks")
    def writebacks(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.PLAN_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(WritebackCommand).order_by(WritebackCommand.created_at.desc()).limit(limit))]

    @app.post("/api/v1/writebacks/{command_id}/retry")
    def retry_writeback(command_id: uuid.UUID, body: ActionBody, ctx=Depends(allowed(security.PLAN_RELEASE))):
        session, p = ctx
        cmd = session.get(WritebackCommand, command_id, with_for_update=True)
        if cmd is None:
            raise NotFound("writeback not found")
        if cmd.status != "dead":
            raise InvalidInput("only dead commands require manual retry")
        cmd.status, cmd.attempts, cmd.next_attempt_at = "sent_unconfirmed", 0, None
        record_audit(session, p, "writeback.retry", "writeback_command", cmd.id, after={"note": body.note, "lookup_first": True})
        return view(cmd)

    @app.get("/api/v1/audit")
    def audit(limit: int = Query(100, ge=1, le=500), ctx=Depends(allowed(security.AUDIT_VIEW))):
        session, _ = ctx
        return [view(r) for r in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))]

    return app


app = create_app()
