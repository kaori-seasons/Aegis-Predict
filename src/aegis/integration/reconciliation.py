"""Exact source manifest reconciliation; independent of the sync cursor / raw digest."""
from __future__ import annotations
import uuid
from sqlalchemy import select
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import canonical_json, content_hash, utcnow
from ..models import BatchRecord, IngestionBatch, IntegrationConnection, RawRecord, SourceAuthority, SourceReconciliation, ReconciliationCase
from .streams import parse_datetime


def _index(records):
    result = {}
    for record in records:
        if set(record) != {'external_id', 'payload', 'source_updated_at'} or not isinstance(record['payload'], dict):
            raise InvalidInput('manifest rows require external_id, payload and source_updated_at')
        key = record['external_id']
        if not isinstance(key, str) or not 1 <= len(key) <= 300:
            raise InvalidInput('invalid manifest external_id')
        try:
            version = parse_datetime(record['source_updated_at'], 'UTC').isoformat() if record['source_updated_at'] else ''
        except (ValueError,TypeError,AttributeError) as exc:
            raise InvalidInput('invalid manifest source_updated_at') from exc
        value = {'version': version, 'payload': record['payload']}
        if key in result and result[key]['version'] == version and result[key] != value:
            raise Conflict('same-version manifest conflict')
        if key not in result or version >= result[key]['version']:
            result[key] = value
    return result


def reconcile(session, principal, batch_id: uuid.UUID, *, source_rows: list[dict], scope: dict,
              evidence: str, execution_refs: dict | None = None):
    batch = session.get(IngestionBatch, batch_id, with_for_update=True)
    if batch is None or batch.tenant_id != principal.tenant_id or batch.status != 'complete':
        raise InvalidInput('reconciliation requires a complete batch in this tenant')
    if not evidence.strip() or scope != batch.scope or len(source_rows) > 50000 or len(canonical_json(source_rows)) > 10_000_000:
        raise InvalidInput('evidence, exact batch scope and a bounded independent source manifest required')
    conn = session.get(IntegrationConnection, batch.connection_id)
    raw = list(session.scalars(select(RawRecord).join(BatchRecord, BatchRecord.raw_record_id == RawRecord.id).where(BatchRecord.batch_id == batch.id)))
    local = _index([{'external_id': r.external_id, 'payload': r.payload, 'source_updated_at': r.source_updated_at} for r in raw])
    source = _index(source_rows)
    missing, extra = sorted(set(source)-set(local)), sorted(set(local)-set(source))
    changed = sorted(key for key in set(source)&set(local) if source[key] != local[key])
    allowed={'sku','seller_sku','location','warehouse','date','as_of','on_hand','in_transit','allocated',
             'backorder','units','quantity','status','order_status','currency','unit_price','updated_at'}
    def preview(value):
        if value is None:return None
        return {'version':value['version'],'payload':{k:(str(v)[:500] if not isinstance(v,(int,float,bool,type(None))) else v)
                for k,v in value['payload'].items() if k in allowed}}
    samples=[{'external_id':key,'kind':kind,'source':preview(source.get(key)),'local':preview(local.get(key)),
              'changed_fields':[k[:100] for k in sorted(set(source.get(key,{}).get('payload',{}))|set(local.get(key,{}).get('payload',{})))
                  if source.get(key,{}).get('payload',{}).get(k)!=local.get(key,{}).get('payload',{}).get(k)][:30]}
             for kind,keys in [('missing',missing),('extra',extra),('changed',changed)] for key in keys[:20]]
    refs = execution_refs or {}
    if len(refs) > 10000:
        raise InvalidInput('too many execution references')
    for key, counters in refs.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(counters, dict) or set(counters) != {'shipped', 'received', 'cancelled'} or any(type(n) is not int or n < 0 for n in counters.values()):
            raise InvalidInput('execution references require nonnegative cumulative integer quantities')
        if counters['received'] > counters['shipped']:
            raise InvalidInput('received exceeds shipped in source evidence')
    details = {'source_count': len(source), 'local_count': len(local), 'missing_count': len(missing),
               'extra_count': len(extra), 'changed_count': len(changed), 'missing': missing[:100],
               'extra': extra[:100], 'changed': changed[:100], 'scope': scope,
               'samples': samples, 'sample_limit_per_kind': 20, 'window_start': batch.window_start.isoformat() if batch.window_start else None,
               'window_end': batch.window_end.isoformat() if batch.window_end else None, 'execution_refs': refs}
    row = SourceReconciliation(tenant_id=principal.tenant_id, batch_id=batch.id, connection_id=conn.id,
        status='mismatch' if missing or extra or changed else 'matched', source_digest=content_hash(source),
        local_digest=content_hash(local), details=details, evidence=evidence.strip(), checked_by=principal.label, is_mock=conn.is_mock, created_at=utcnow())
    session.add(row); session.flush()
    record_audit(session, principal, 'data.reconcile', 'source_reconciliation', row.id,
                 after={'batch_id': batch.id, 'status': row.status, 'source_digest': row.source_digest})
    # Keep the operational queue open when a new independent check finds a regression.
    case=session.scalar(select(ReconciliationCase).where(ReconciliationCase.batch_id==batch.id).with_for_update())
    if row.status=='mismatch' and case and case.status=='resolved':
        case.status='open';case.resolution_check_id=None;case.revision+=1
        record_audit(session,principal,'data.disposition.reopen','reconciliation_case',case.id,
                     after={'check_id':row.id,'revision':case.revision,'note':'New source reconciliation mismatch'})
    return row


def latest_check(session, batch_id):
    return session.scalar(select(SourceReconciliation).where(SourceReconciliation.batch_id == batch_id)
        .order_by(SourceReconciliation.created_at.desc(), SourceReconciliation.id.desc()).limit(1))


def require_reconciled(session, tenant_id, *, live: bool = False):
    from .observations import latest_batch
    checks = []
    for rule in session.scalars(select(SourceAuthority).where(SourceAuthority.tenant_id == tenant_id)):
        batch = latest_batch(session, rule)
        check = latest_check(session, batch.id)
        if check is None or check.status != 'matched' or (live and check.is_mock):
            raise Conflict('latest authoritative batch lacks a matching source reconciliation',
                           code='source_reconciliation_required', details={'batch_id': str(batch.id), 'domain': rule.domain})
        checks.append(str(check.id))
    return sorted(set(checks))
