"""Tenant-bound observation pool and audit-preserving launch/context mapping writes."""

from __future__ import annotations

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from ..core import security as sec
from ..core.audit import record_audit
from ..core.errors import Conflict, InvalidInput
from ..core.util import content_hash, utcnow
from ..integration.commerce import IDENTITIES, TABLES, append_revision, build_values
from ..integration.connectors.public_market import PROVIDERS
from ..integration.mapping import MappingContext, MappingIssue
from ..integration.raw import land_batch
from ..integration.spi import RawItem
from ..integration.streams import STREAMS, parse_record
from ..models import IntegrationConnection, RawRecord


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_system: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    record: dict = Field(max_length=20)


def write_revision(session, p, stream, body):
    connection = session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.source_system == body.source_system,
            IntegrationConnection.status == "active",
        )
    )
    if connection is None:
        raise InvalidInput("active source not found")
    if any(
        k in body.record
        for k in (
            "received_at",
            "available_at",
            "revision",
            "tenant_id",
            "source_system",
        )
    ):
        raise InvalidInput("server-managed fields are forbidden")
    now = utcnow()
    payload = {
        **body.record,
        "revision": body.expected_revision + 1,
        "published_at": now.isoformat(),
    }
    try:
        ctx = MappingContext.load(session, p.tenant_id, body.source_system)
        parsed = parse_record(STREAMS[stream], payload, "UTC")
        values = build_values(stream, parsed, ctx)
    except (ValueError, KeyError, MappingIssue) as e:
        raise InvalidInput(str(e)) from e
    identity = {k: values[k] for k in IDENTITIES[stream]}
    key = content_hash([str(p.tenant_id), body.source_system, stream, identity])
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"), {"k": key}
    )
    model = TABLES[stream]
    latest = session.scalar(
        select(model)
        .where(
            model.source_system == body.source_system,
            *[getattr(model, k) == v for k, v in identity.items()],
        )
        .order_by(model.revision.desc())
        .limit(1)
    )
    if (latest.revision if latest else 0) != body.expected_revision:
        raise Conflict("revision changed; reload before editing")
    external = content_hash([stream, identity, payload])
    land_batch(
        session,
        tenant_id=p.tenant_id,
        connection_id=connection.id,
        sync_run_id=None,
        items=[RawItem(stream, external, payload, now)],
    )
    raw = session.scalar(
        select(RawRecord)
        .where(
            RawRecord.connection_id == connection.id,
            RawRecord.stream == stream,
            RawRecord.external_id == external,
        )
        .order_by(RawRecord.received_at.desc())
        .limit(1)
    )
    try:
        append_revision(
            session, p.tenant_id, body.source_system, stream, raw, values, now
        )
    except ValueError as e:
        raise InvalidInput(str(e)) from e
    raw.normalize_status = "ok"
    raw.normalized_at = now
    raw.mapper_version = "commerce-manual-v1"
    ctx.flush_pending(session)
    record_audit(session, p, "commerce.revision", stream, raw.id, after=payload)
    session.flush()
    return {"raw_id": str(raw.id), "revision": values["revision"]}


def install(app, allowed, view):
    @app.get("/api/v1/commerce/summary")
    def summary(ctx=Depends(allowed(sec.DATA_VIEW))):
        s, _ = ctx
        result = {"providers": PROVIDERS}
        for stream, model in TABLES.items():
            result[stream] = [
                view(r)
                for r in s.scalars(
                    select(model).order_by(model.available_at.desc()).limit(200)
                )
            ]
        return result

    @app.post("/api/v1/commerce/launches", status_code=201)
    def launch(body: RevisionBody, ctx=Depends(allowed(sec.CONNECTION_EDIT))):
        s, p = ctx
        return write_revision(s, p, "sku_launches", body)

    @app.post("/api/v1/commerce/bindings", status_code=201)
    def binding(body: RevisionBody, ctx=Depends(allowed(sec.CONNECTION_EDIT))):
        s, p = ctx
        return write_revision(s, p, "public_signal_bindings", body)

    @app.post("/api/v1/commerce/profiles",status_code=201)
    def profile(body:RevisionBody,ctx=Depends(allowed(sec.CONNECTION_EDIT))):
        s,p=ctx
        return write_revision(s,p,"launch_profiles",body)
