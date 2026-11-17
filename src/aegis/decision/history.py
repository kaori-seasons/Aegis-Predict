"""Replay canonical facts using a recorded PostgreSQL committed-visibility snapshot.

Temporary, transaction-local, typed projections let the SAME authority/coverage and
inventory validation code assemble historical inputs. No live canonical table may
be consulted inside the replay. Publication time is an additional market filter.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from sqlalchemy import text

from ..core.errors import InvalidInput, NotFound
from ..core.util import content_hash, json_safe, utcnow
from ..models import DecisionArtifact, KnowledgeCheckpoint

TRACKED = (
    "sku",
    "location",
    "sku_component",
    "lane",
    "transport_option",
    "supplier_terms",
    "inventory_snapshot",
    "purchase_order_line",
    "order_line",
    "demand_observation",
    "source_authority",
    "id_mapping",
    "integration_connection",
    "ingestion_batch",
    "ingestion_batch_record",
    "inventory_inbound_link",
    "inventory_reservation",
    "market_signal_revision",
    "raw_record",
    "sync_stream_state",
    "plan_line_pin",
)
MAX_REPLAY_ROWS = 250000


def checkpoint(session, tenant_id):
    # BEFORE INSERT stamps both fields on the server; clients cannot backdate knowledge.
    row = KnowledgeCheckpoint(tenant_id=tenant_id, captured_at=utcnow(), pg_snapshot="")
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def require_checkpoint(session, ident):
    row = session.get(KnowledgeCheckpoint, ident)
    if row is None:
        raise NotFound("knowledge checkpoint not found")
    return row


def facts(session, cp, table):
    if table not in (*TRACKED, "decision_artifact"):
        raise InvalidInput("untracked fact table")
    rows = session.execute(
        text("""SELECT payload, operation, id FROM (
      SELECT payload, operation, id, row_number() OVER(PARTITION BY entity_key ORDER BY id DESC) n
      FROM public.fact_revision WHERE tenant_id=:tenant AND table_name=:table
        AND available_at <= :cutoff
        AND pg_visible_in_snapshot(transaction_id::xid8, CAST(:snapshot AS pg_snapshot))
      ) revisions WHERE n=1 ORDER BY id LIMIT :lim"""),
        {
            "tenant": cp.tenant_id,
            "table": table,
            "cutoff": cp.captured_at,
            "snapshot": cp.pg_snapshot,
            "lim": MAX_REPLAY_ROWS + 1,
        },
    ).all()
    if len(rows) > MAX_REPLAY_ROWS:
        raise InvalidInput(
            "history replay budget exceeded; use a bounded dedicated replica"
        )
    return [(r.payload, r.id) for r in rows if r.operation != "DELETE"]


def artifact(
    session,
    tenant_id,
    kind,
    identity,
    payload,
    *,
    checkpoint_id=None,
    parent_id=None,
    actor=None,
):
    payload = json_safe(payload)
    row = DecisionArtifact(
        tenant_id=tenant_id,
        kind=kind,
        identity=identity,
        payload=payload,
        digest=content_hash(payload),
        checkpoint_id=checkpoint_id,
        parent_id=parent_id,
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def verified(session, ident, kind=None):
    row = session.get(DecisionArtifact, ident)
    if row is None or (kind and row.kind != kind):
        raise NotFound("decision evidence not found")
    if content_hash(row.payload) != row.digest:
        raise InvalidInput("decision evidence digest mismatch")
    return row


@contextmanager
def replay(session, cp):
    if session.info.get("decision_replay"):
        raise InvalidInput("nested history replay is unsupported")
    # A caller may have loaded today's ORM objects before choosing an older
    # checkpoint. Never let the identity map override the replay projections.
    changed = [
        o
        for o in (*session.new, *session.dirty, *session.deleted)
        if getattr(o, "__tablename__", None) in TRACKED
    ]
    if changed:
        raise InvalidInput("commit canonical mutations before historical replay")
    for obj in list(session.identity_map.values()):
        if getattr(obj, "__tablename__", None) in TRACKED:
            session.expunge(obj)
    session.info["decision_replay"] = True
    made = []
    provenance = {}
    total_rows = 0
    bom_rows = facts(session, cp, "decision_artifact")
    try:
        for name in TRACKED:
            rows = facts(session, cp, name)
            total_rows += len(rows)
            if total_rows > MAX_REPLAY_ROWS:
                raise InvalidInput("total historical fact replay budget exceeded")
            provenance[name] = [ident for _, ident in rows]
            session.execute(
                text(
                    f"CREATE TEMP TABLE {name} ON COMMIT DROP AS SELECT * FROM public.{name} WHERE false"
                )
            )
            made.append(name)
            if rows:
                session.execute(
                    text(
                        f"INSERT INTO pg_temp.{name} SELECT * FROM jsonb_populate_recordset(NULL::public.{name}, CAST(:rows AS jsonb))"
                    ),
                    {"rows": json.dumps([p for p, _ in rows])},
                )
        # Recreate order-derived component demand using the BOM effective at each
        # order's business time AND known at this checkpoint; never today's BOM.
        from .bundles import rebuild_order_demand

        rebuild_order_demand(session, cp, [p for p, _ in bom_rows])
        provenance["bom_versions"] = [i for _, i in bom_rows]
        yield provenance
    finally:
        for obj in list(session.identity_map.values()):
            if getattr(obj, "__tablename__", None) in TRACKED:
                session.expunge(obj)
        for name in reversed(made):
            session.execute(text(f"DROP TABLE IF EXISTS pg_temp.{name}"))
        session.info.pop("decision_replay", None)


def freeze_input(session, principal, cp, policy):
    from ..planning.assembler import assemble

    with replay(session, cp) as provenance:
        built = assemble(session, principal.tenant_id, cp.captured_at, policy)
        from .bundles import inbound_timeline, validate_stock_shape

        validate_stock_shape(session, cp, built.payload)
        arrivals = inbound_timeline(session, cp, built.payload, built.ids)
    payload = {
        "schema": "pit-network-v1",
        "network": built.payload,
        "ids": built.ids,
        "pins": built.pins,
        "policy": policy.model_dump(),
        "watermarks": built.watermarks,
        "freshness": built.freshness,
        "stale": built.stale,
        "checkpoint_id": str(cp.id),
        "as_of": cp.captured_at,
        "fact_revisions": provenance,
        "arrivals": arrivals,
        "history_semantics": "committed-visibility-forward-only",
    }
    return artifact(
        session,
        principal.tenant_id,
        "input",
        uuid.uuid4().hex,
        payload,
        checkpoint_id=cp.id,
        actor=principal.user_id,
    )
