"""Tenant-bound administration services and paged read models."""
from __future__ import annotations
import secrets
from sqlalchemy import select, func, or_, any_, exists
from ..core import security
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput, NotFound
from ..core.util import utcnow
from ..models import (AppUser, Tenant, ExternalIdentity, Location, Sku, IdMapping, RawRecord,
    QuarantineRecord, IngestionBatch, SourceReconciliation, ReconciliationCase, AuditLog)
from .workspace import public


def page(session, query, limit, offset):
    total = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {'items': [public(r) for r in session.scalars(query.limit(limit).offset(offset))],
            'total': total, 'limit': limit, 'offset': offset}


def validate_member(session, roles, scope):
    roles = security.validate_roles(roles)
    if not roles:
        raise InvalidInput('at least one role required')
    if scope is not None:
        if len(scope)>500 or len(set(scope))!=len(scope):
            raise InvalidInput('invalid location scope')
        valid=set(session.scalars(select(Location.code).where(Location.is_active, Location.code.in_(scope))))
        if valid!=set(scope):
            raise InvalidInput('location scope contains unknown or inactive tenant locations')
    if 'TENANT_ADMIN' in roles and scope is not None:
        raise InvalidInput('tenant administrators require all-location scope')
    return roles


def members(session, *, q='', limit=50, offset=0):
    query=select(AppUser).order_by(AppUser.email)
    if q: query=query.where(AppUser.email.icontains(q, autoescape=True) | AppUser.display_name.icontains(q, autoescape=True))
    result=page(session,query,limit,offset)
    bound={str(x) for x in session.scalars(select(ExternalIdentity.user_id).where(ExternalIdentity.provider=='google'))}
    for row in result['items']:
        row['google_bound']=str(row['id']) in bound
        row['permissions']=sorted(security.permissions_for(row['roles']))
    return result


def create_member(session, principal, *, email, password=None, roles, location_scope=None, display_name='', google_subject=None):
    # Serialize membership changes so the last-admin invariant also holds under concurrent requests.
    session.get(Tenant,principal.tenant_id,with_for_update=True)
    email=email.strip().lower()
    if not email or '@' not in email or any(c.isspace() for c in email):
        raise InvalidInput('valid email required')
    roles=validate_member(session,roles,location_scope)
    if security.get_settings().auth_mode=='password' and not password:
        raise InvalidInput('password required in password authentication mode')
    user=AppUser(tenant_id=principal.tenant_id,email=email,display_name=display_name.strip(),roles=roles,
        location_scope=location_scope,password_hash=security.hash_password(password or secrets.token_urlsafe(48)))
    session.add(user);session.flush()
    if google_subject:
        from ..auth.google import bind_identity
        bind_identity(session,principal,user.id,google_subject)
    record_audit(session,principal,'user.create','app_user',user.id,after={'email':user.email,'roles':roles,'location_scope':location_scope})
    return user


def update_member(session, principal, user_id, *, expected_updated_at, note, **changes):
    session.get(Tenant,principal.tenant_id,with_for_update=True)
    user=session.get(AppUser,user_id,with_for_update=True)
    if user is None: raise NotFound('member not found')
    if expected_updated_at!=user.updated_at: raise Conflict('member changed; refresh before editing')
    if not note.strip(): raise InvalidInput('reason required')
    roles=validate_member(session,changes.get('roles',user.roles),changes.get('location_scope',user.location_scope))
    active=changes.get('is_active',user.is_active)
    candidates=select(func.count()).select_from(AppUser).where(AppUser.id!=user.id,
        AppUser.is_active,'TENANT_ADMIN' == any_(AppUser.roles),AppUser.location_scope.is_(None))
    if security.get_settings().auth_mode=='google':
        candidates=candidates.where(exists(select(ExternalIdentity.id).where(ExternalIdentity.user_id==AppUser.id,ExternalIdentity.provider=='google')))
    remaining=session.scalar(candidates)
    if user.is_active and 'TENANT_ADMIN' in user.roles and (not active or 'TENANT_ADMIN' not in roles) and not remaining:
        raise Conflict('cannot disable or demote the last active tenant administrator')
    before={'roles':user.roles,'is_active':user.is_active,'location_scope':user.location_scope,'display_name':user.display_name}
    for key,value in changes.items(): setattr(user,key,value.strip() if key=='display_name' else value)
    user.roles=roles;user.updated_at=utcnow()
    security.revoke_user_sessions(session,user.id)
    record_audit(session,principal,'user.update','app_user',user.id,before=before,
        after={**changes,'note':note.strip(),'sessions_revoked':True})
    session.flush();return user


def mapping_rows(session, *, q='', status=None, entity=None, limit=50, offset=0):
    query=select(IdMapping).order_by(IdMapping.created_at.desc(),IdMapping.id)
    if q:query=query.where(or_(IdMapping.external_id.icontains(q,autoescape=True),IdMapping.source_system.icontains(q,autoescape=True)))
    if status:query=query.where(IdMapping.status==status)
    if entity:query=query.where(IdMapping.entity==entity)
    result=page(session,query,limit,offset)
    for row in result['items']:
        model=Sku if row['entity']=='sku' else Location
        target=session.get(model,row['target_id']) if row['target_id'] else None
        row['target_code']=target.code if target else None
    return result


def reconciliation_rows(session, *, q='', status=None, limit=50, offset=0):
    latest=select(SourceReconciliation.id,func.row_number().over(partition_by=SourceReconciliation.batch_id,
        order_by=(SourceReconciliation.created_at.desc(),SourceReconciliation.id.desc())).label('rn')).subquery()
    query=select(SourceReconciliation).join(latest,latest.c.id==SourceReconciliation.id).where(latest.c.rn==1)
    if status:query=query.where(SourceReconciliation.status==status)
    if q:query=query.where(SourceReconciliation.evidence.icontains(q,autoescape=True))
    query=query.order_by(SourceReconciliation.created_at.desc(),SourceReconciliation.id)
    result=page(session,query,limit,offset)
    for row in result['items']:
        batch=session.get(IngestionBatch,row['batch_id'])
        case=session.scalar(select(ReconciliationCase).where(ReconciliationCase.batch_id==batch.id))
        assignee=session.get(AppUser,case.assignee_id) if case and case.assignee_id else None
        row.update(stream=batch.stream,case=public(case) if case else None,assignee_label=assignee.email if assignee else None)
    return result


def reconciliation_detail(session, check_id):
    check=session.get(SourceReconciliation,check_id)
    if check is None:raise NotFound('reconciliation not found')
    case=session.scalar(select(ReconciliationCase).where(ReconciliationCase.batch_id==check.batch_id))
    history=list(session.scalars(select(SourceReconciliation).where(SourceReconciliation.batch_id==check.batch_id)
        .order_by(SourceReconciliation.created_at.desc(),SourceReconciliation.id.desc()).limit(20)))
    notes=list(session.scalars(select(AuditLog).where(AuditLog.resource_type=='reconciliation_case',
        AuditLog.resource_id==str(case.id)).order_by(AuditLog.id.desc()).limit(50))) if case else []
    return {'check':public(check),'case':public(case) if case else None,'history':[public(r) for r in history],
        'notes':[public(r) for r in notes],'batch':public(session.get(IngestionBatch,check.batch_id))}


def disposition(session, principal, batch_id, *, status, expected_revision, assignee_id=None, resolution_check_id=None, note):
    from ..integration.reconciliation import latest_check
    batch=session.get(IngestionBatch,batch_id,with_for_update=True)
    if batch is None:raise NotFound('batch not found')
    case=session.scalar(select(ReconciliationCase).where(ReconciliationCase.batch_id==batch.id).with_for_update())
    if (case.revision if case else 0)!=expected_revision:raise Conflict('case changed; refresh before editing')
    if not note.strip():raise InvalidInput('disposition reason required')
    if not session.scalar(select(SourceReconciliation.id).where(SourceReconciliation.batch_id==batch.id,SourceReconciliation.status=='mismatch').limit(1)):
        raise InvalidInput('no reconciliation difference to disposition')
    if assignee_id:
        user=session.get(AppUser,assignee_id)
        if user is None or not user.is_active or security.CONNECTION_EDIT not in security.permissions_for(user.roles) or user.location_scope is not None:
            raise InvalidInput('assignee must be an active tenant integration administrator')
    if status=='resolved':
        check=session.get(SourceReconciliation,resolution_check_id) if resolution_check_id else None
        target=session.get(IngestionBatch,check.batch_id) if check else None
        if not check or check.status!='matched' or latest_check(session,check.batch_id).id!=check.id:
            raise Conflict('resolution requires the latest matching reconciliation')
        if (target.connection_id,target.stream,target.scope)!=(batch.connection_id,batch.stream,batch.scope):
            raise Conflict('corrective batch must cover the same source, stream and scope')
        if target.id!=batch.id:
            newest=session.scalar(select(IngestionBatch.id).where(IngestionBatch.connection_id==batch.connection_id,
                IngestionBatch.stream==batch.stream,IngestionBatch.status=='complete').order_by(IngestionBatch.completed_at.desc(),IngestionBatch.id.desc()).limit(1))
            if target.id!=newest or target.created_at<batch.created_at or (batch.window_start and (not target.window_start or target.window_start>batch.window_start)) or (batch.window_end and (not target.window_end or target.window_end<batch.window_end)):
                raise Conflict('corrective batch must be current and cover the original time window')
    if case is None:
        case=ReconciliationCase(tenant_id=principal.tenant_id,batch_id=batch.id,revision=0)
        session.add(case)
    case.status=status;case.assignee_id=assignee_id;case.resolution_check_id=resolution_check_id if status=='resolved' else None;case.revision+=1
    session.flush()
    record_audit(session,principal,'data.disposition','reconciliation_case',case.id,
        after={'status':status,'revision':case.revision,'assignee_id':assignee_id,'resolution_check_id':case.resolution_check_id,'note':note.strip()})
    return case
