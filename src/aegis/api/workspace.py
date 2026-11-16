"""Read models for the tenant workspace. No synthetic metrics or mutable global UI state."""
from __future__ import annotations
from sqlalchemy import select,func,inspect
from ..core.errors import AegisError,InvalidInput,NotFound
from ..core.util import json_safe,utcnow
from ..models import (Sku,Location,SourceAuthority,IntegrationConnection,InventorySnapshot,IngestionBatch,
    PlanRun,PlanLine,InventoryReservation,ExecutionObservation,InventorySettlement,WritebackCommand,Tenant)
from ..integration.observations import latest_batch
from ..planning.lifecycle import get_plan,read_frozen


def public(row):
    omitted={'secret_ciphertext','password_hash','token_hash','lease_owner','lease_expires_at'}
    return json_safe({a.key:getattr(row,a.key) for a in inspect(row).mapper.column_attrs if a.key not in omitted})


def overview(session,principal):
    tenant=session.get(Tenant,principal.tenant_id)
    rules=list(session.scalars(select(SourceAuthority).where(SourceAuthority.domain=='inventory')))
    stock=[];issues=[]
    sku_count=session.scalar(select(func.count()).select_from(Sku).where(Sku.is_active))
    location_count=session.scalar(select(func.count()).select_from(Location).where(Location.is_active))
    for rule in rules:
        conn=session.get(IntegrationConnection,rule.connection_id)
        try:
            batch=latest_batch(session,rule)
            ranked=select(InventorySnapshot.on_hand,InventorySnapshot.in_transit,
                func.row_number().over(partition_by=InventorySnapshot.sku_id,
                    order_by=(InventorySnapshot.as_of.desc(),InventorySnapshot.ingested_at.desc())).label('rn')).join(Sku,Sku.id==InventorySnapshot.sku_id).where(
                    Sku.is_active,InventorySnapshot.location_id==rule.location_id,InventorySnapshot.ingestion_batch_id==batch.id).subquery()
            rows=session.execute(select(func.coalesce(func.sum(ranked.c.on_hand),0),
                func.coalesce(func.sum(ranked.c.in_transit),0),func.count()).where(ranked.c.rn==1)).one()
            location=session.get(Location,rule.location_id)
            stock.append({'location_id':str(location.id),'code':location.code,'name':location.name or location.code,
                'kind':location.kind,'on_hand':rows[0],'in_transit':rows[1],'snapshot_count':rows[2],
                'batch_id':str(batch.id),'is_mock':conn.is_mock,'completed_at':batch.completed_at})
        except AegisError as exc:
            issues.append({'location_id':str(rule.location_id),'message':exc.message})
    return json_safe({'tenant':{'id':tenant.id,'name':tenant.name,'slug':tenant.slug},'stock':stock,'issues':issues,
        'sku_count':sku_count,
        'warehouse_count':session.scalar(select(func.count()).select_from(Location).where(Location.is_active,Location.kind!='pool')),
        'inventory_complete':bool(rules) and len(stock)==location_count and not issues and all(s['snapshot_count']==sku_count for s in stock),
        'as_of':utcnow()})


def catalog(session,*,limit=500,offset=0):
    return {'locations':[public(r) for r in session.scalars(select(Location).where(Location.is_active).order_by(Location.code).limit(500))],
            'skus':[public(r) for r in session.scalars(select(Sku).where(Sku.is_active).order_by(Sku.code).offset(offset).limit(limit))],
            'sku_total':session.scalar(select(func.count()).select_from(Sku).where(Sku.is_active)),'limit':limit,'offset':offset}


def plan_forecast(session,plan_id,line_id,horizon):
    from overseas_inventory.models import network_from_payload
    from ..planning.forecast import forecast
    plan=get_plan(session,plan_id)
    line=session.scalar(select(PlanLine).where(PlanLine.id==line_id,PlanLine.plan_run_id==plan.id))
    if line is None:raise NotFound('plan line unavailable')
    frozen=read_frozen(session,plan)
    network=network_from_payload(frozen['network'])
    series=next((s for s in network.demand_history if s.sku==line.sku_code and s.warehouse_id==line.location_code),None)
    if series is None:raise InvalidInput('frozen demand series unavailable')
    fc=forecast(series,horizon,cold_start_rate=line.daily_rate)
    return {'plan_id':str(plan.id),'input_digest':plan.input_digest,'cutoff_at':plan.cutoff_at,'forecast':fc.to_dict(),
        'history':{'units':series.daily_units[-90:],'missing':(series.missing_days or [False]*len(series.daily_units))[-90:],
                   'censored':(series.stockout_days or [False]*len(series.daily_units))[-90:]},
        'quantile_method':'normal approximation; not a calibrated service guarantee','inventory_position':line.inventory_position}


def evaluation(session,plan_id):
    from overseas_inventory.models import network_from_payload
    from ..planning.backtest import rolling_backtest
    plan=get_plan(session,plan_id)
    frozen=read_frozen(session,plan)
    if len(frozen['network'].get('demand_history',[]))>1000:
        raise InvalidInput('interactive backtest limited to 1000 series; use the offline evaluation job')
    result=rolling_backtest(network_from_payload(frozen['network']))
    return {'plan_id':str(plan.id),'input_digest':plan.input_digest,'metrics':result['metrics'],
            'horizon':result['horizon'],'min_train':result['min_train'],'summary':plan.summary,
            'method':'training-only rolling holdout; excludes missing/censored test windows'}


def execution(session,plan_id):
    plan=get_plan(session,plan_id)
    reservations=list(session.scalars(select(InventoryReservation).join(PlanLine,PlanLine.id==InventoryReservation.plan_line_id)
        .where(PlanLine.plan_run_id==plan.id).order_by(InventoryReservation.created_at)))
    rows=[]
    for reservation in reservations:
        line=session.get(PlanLine,reservation.plan_line_id)
        event=session.scalar(select(ExecutionObservation).where(ExecutionObservation.reservation_id==reservation.id)
            .order_by(ExecutionObservation.source_version.desc()).limit(1))
        settled=session.scalar(select(InventorySettlement).where(InventorySettlement.observation_id==event.id)) if event else None
        authority=session.scalar(select(SourceAuthority).where(SourceAuthority.domain=='inventory',SourceAuthority.location_id==reservation.location_id))
        candidates={}
        for label,loc in [('source',reservation.location_id),('destination',line.location_id)]:
            rule=session.scalar(select(SourceAuthority).where(SourceAuthority.domain=='inventory',SourceAuthority.location_id==loc))
            if rule:
                try:candidates[label]=str(latest_batch(session,rule).id)
                except AegisError:pass
        rows.append({'reservation':public(reservation),'sku':line.sku_code,'location':line.location_code,
            'execution_connection_id':str(plan.writeback_connection_id or authority.connection_id) if plan.writeback_connection_id or authority else None,
            'latest':public(event) if event else None,'settlement':public(settled) if settled else None,'candidate_batches':candidates})
    return {'reservations':rows,'commands':[public(c) for c in session.scalars(select(WritebackCommand)
            .where(WritebackCommand.plan_run_id==plan.id).order_by(WritebackCommand.created_at))]}
