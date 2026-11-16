"""Planning reads source-separated facts selected by approved coverage rules, never blended totals."""
from __future__ import annotations
from collections import defaultdict
from datetime import UTC, datetime, time
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..core.errors import InvalidInput
from ..models import DemandObservation, IngestionBatch, IntegrationConnection, SourceAuthority


def authorities(session: Session, tenant_id):
    return list(session.scalars(select(SourceAuthority).where(SourceAuthority.tenant_id == tenant_id)))


def latest_batch(session: Session, rule: SourceAuthority):
    batch = session.scalar(select(IngestionBatch).where(IngestionBatch.connection_id == rule.connection_id,
        IngestionBatch.stream == rule.stream).order_by(IngestionBatch.created_at.desc(), IngestionBatch.id.desc()).limit(1))
    if batch is None or batch.status != "complete":
        raise InvalidInput("authoritative stream has no complete latest batch", code="incomplete_authoritative_stream",
                           details={"connection_id": str(rule.connection_id), "stream": rule.stream,
                                    "batch_status": batch.status if batch else "missing"})
    connection = session.get(IntegrationConnection, rule.connection_id)
    if connection is None or connection.status != "active":
        raise InvalidInput("authoritative connection is not active")
    return batch


def load_demand(session: Session, tenant_id, rules: list[SourceAuthority], since):
    """Return daily facts after selecting exactly one source per declared channel.

    A row has total units plus independent missing/censored markers. Missing channels do not
    vanish into a partial total; a day's completeness is computed later against every rule.
    """
    source_by_connection = {c.id: c.source_system for c in session.scalars(select(IntegrationConnection))}
    observations = defaultdict(dict)
    coverage = defaultdict(list)
    for rule in rules:
        if rule.domain != "demand":
            continue
        batch = latest_batch(session, rule)
        source = source_by_connection[rule.connection_id]
        query = select(DemandObservation).where(DemandObservation.tenant_id == tenant_id,
            DemandObservation.location_id == rule.location_id, DemandObservation.source_system == source,
            DemandObservation.kind == rule.stream, DemandObservation.demand_date >= since)
        if rule.channel != "*":
            query = query.where(DemandObservation.channel == rule.channel)
        selected = list(session.scalars(query))
        seen = defaultdict(set)
        for row in selected:
            key = (row.sku_id, row.location_id)
            seen[(key, row.demand_date)].add(row.channel)
            day = observations[key].setdefault(row.demand_date, {"units": 0, "censored": False, "missing": False, "rules": set()})
            day["units"] += row.units
            day["censored"] |= row.state == "censored"
            day["missing"] |= row.state == "missing"
            day["rules"].add(rule.id)
        if any("*" in channels and len(channels) > 1 for channels in seen.values()):
            raise InvalidInput("aggregate and channel totals overlap within an authoritative source")
        intervals = []
        if rule.stream == "order_lines":
            for b in session.scalars(select(IngestionBatch).where(IngestionBatch.connection_id == rule.connection_id,
                                      IngestionBatch.stream == rule.stream, IngestionBatch.status == "complete")):
                if b.scope.get("order_window") and b.window_start is not None and b.window_end is not None:
                    intervals.append((b.window_start, b.window_end))
        coverage[rule.location_id].append((rule, sorted(intervals)))
    return observations, coverage


def zero_is_proven(rule, intervals, day, timezone) -> bool:
    if rule.stream != "order_lines" or rule.coverage_start is None or day < rule.coverage_start:
        return False
    from datetime import timedelta
    start = datetime.combine(day, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
    cursor = start
    for lo, hi in intervals:
        if lo > cursor:
            break
        if hi > cursor:
            cursor = hi
        if cursor >= end:
            return True
    return False
