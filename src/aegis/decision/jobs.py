"""Independent, bounded, idempotent forecast queue with transactional fencing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from ..core.audit import record_audit
from ..core.db import admin_session, tenant_session
from ..core.errors import Conflict, InvalidInput
from ..core.tenancy import system_principal
from ..core.util import content_hash, sha256_hex, utcnow
from ..models import ForecastJob
from ..planning.policy import PlanningPolicy
from .bundles import lock
from .forecasting import MODELS, predict, required_horizons, scope
from .history import artifact, checkpoint, freeze_input, require_checkpoint, verified


# Digest every kernel source used by new artifacts, not an operator-supplied label.
def code_digest():
    root = Path(__file__).parent.parent
    files = [
        *sorted((root / "planning").glob("*.py")),
        *sorted((root / "decision").glob("*.py")),
    ]
    return sha256_hex(b"".join(p.name.encode() + p.read_bytes() for p in files))


def request(
    session, p, key, horizons, models, policy, checkpoint_id=None, calibration_id=None
):
    lock(session, p.tenant_id)
    signature = content_hash(
        {
            "key": key,
            "horizons": horizons,
            "models": models,
            "policy": policy,
            "checkpoint_id": checkpoint_id,
            "calibration_id": calibration_id,
        }
    )
    existing = session.scalar(
        select(ForecastJob).where(ForecastJob.idempotency_key == key)
    )
    if existing:
        if existing.request_hash != signature:
            raise Conflict("idempotency key already used with a different request")
        return existing
    if (
        session.scalar(
            select(func.count())
            .select_from(ForecastJob)
            .where(ForecastJob.status.in_(("queued", "running")))
        )
        >= 4
    ):
        raise Conflict("tenant forecast queue limit reached")
    if not models or any(m not in MODELS for m in models):
        raise InvalidInput("unsupported forecast model")
    config = PlanningPolicy.model_validate(policy)
    models = sorted(set([*models, "mean_28", config.forecast_model_version]))
    if "commerce-coldstart-v5" in models:
        models = sorted(set([*models, "rolling-champion-v3"]))
    if "market-aware-v4" in models and not (
        config.market_signal_source and config.market_signal_platform
    ):
        raise InvalidInput(
            "market model requires an explicit approved provider and platform"
        )
    cp = (
        require_checkpoint(session, checkpoint_id)
        if checkpoint_id
        else checkpoint(session, p.tenant_id)
    )
    inp = freeze_input(session, p, cp, config)
    try:
        horizons = required_horizons(inp.payload, horizons)
    except ValueError as e:
        raise InvalidInput(str(e)) from e
    from .evaluation import champion_for

    models = sorted(set([*models, champion_for(session, scope(inp.payload, horizons))]))
    n = len(inp.payload["network"]["demand_history"])
    if ("commerce-coldstart-v5" in models and n>30) or n > 200 or n * len(models) * max(horizons) > 300000:
        raise InvalidInput("forecast budget exceeded; narrow the tenant planning scope")
    calibration = None
    if calibration_id:
        cal = verified(session, calibration_id, "calibration")
        if (
            not session.scalar(
                text(
                    "SELECT pg_visible_in_snapshot(CAST(:x AS xid8),CAST(:s AS pg_snapshot))"
                ),
                {"x": cal.transaction_id, "s": cp.pg_snapshot},
            )
            or cal.available_at > cp.captured_at
        ):
            raise InvalidInput(
                "calibration was not committed and available at the selected checkpoint"
            )
        if cal.payload["code_digest"] != code_digest():
            raise InvalidInput("calibration requires the identical model code image")
        if cal.created_at > cp.captured_at or cal.payload["scope"] != scope(
            inp.payload, horizons
        ):
            raise InvalidInput("calibration is future-known or scope mismatched")
        calibration = str(cal.id)
    # Input artifact is immutable; job metadata carries explicit calibration selection.
    row = ForecastJob(
        tenant_id=p.tenant_id,
        idempotency_key=key,
        request_hash=signature,
        input_id=inp.id,
        horizons=horizons,
        model_versions=models,
        created_by=p.user_id,
        status="queued",
        scope_key=scope(inp.payload, horizons),
        kernel_digest=code_digest(),
    )
    # Separate immutable task manifest preserves reproducibility across code deployments.
    manifest = artifact(
        session,
        p.tenant_id,
        "input",
        f"manifest:{row.id or uuid.uuid4()}",
        {
            "schema": "forecast-task-v1",
            "input_id": str(inp.id),
            "input_digest": inp.digest,
            "calibration_id": calibration,
            "code_digest": code_digest(),
            "models": models,
            "horizons": horizons,
        },
        parent_id=inp.id,
        actor=p.user_id,
    )
    row.input_id = manifest.id
    session.add(row)
    session.flush()
    record_audit(
        session,
        p,
        "forecast.request",
        "forecast_job",
        row.id,
        after={"input_id": manifest.id, "horizons": horizons, "models": models},
    )
    return row


@dataclass(frozen=True)
class Claim:
    id: uuid.UUID
    tenant_id: uuid.UUID
    owner: str


def claim():
    with admin_session() as session:
        row = session.scalar(
            select(ForecastJob)
            .where(ForecastJob.status == "queued")
            .order_by(ForecastJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status = "running"
        row.attempts += 1
        row.lease_owner = uuid.uuid4().hex
        row.lease_expires_at = utcnow() + timedelta(minutes=10)
        return Claim(row.id, row.tenant_id, row.lease_owner)


def fenced(session, c):
    row = session.get(ForecastJob, c.id, with_for_update=True)
    if (
        row is None
        or row.status != "running"
        or row.lease_owner != c.owner
        or row.lease_expires_at <= utcnow()
    ):
        raise Conflict("forecast worker lease lost")
    return row


def execute(c):
    try:
        with tenant_session(c.tenant_id) as session:
            row = fenced(session, c)
            manifest = verified(session, row.input_id, "input").payload
            if manifest["code_digest"] != code_digest():
                raise InvalidInput(
                    "forecast code version changed; create a new run, do not silently replay with different code"
                )
            inp = verified(session, uuid.UUID(manifest["input_id"]), "input")
            if inp.digest != manifest["input_digest"]:
                raise InvalidInput("forecast input digest mismatch")
            data = inp.payload
            cal = (
                verified(
                    session, uuid.UUID(manifest["calibration_id"]), "calibration"
                ).payload
                if manifest["calibration_id"]
                else None
            )
        result = predict(data, manifest["horizons"], manifest["models"], cal)
        result.update(
            input_id=manifest["input_id"],
            input_digest=manifest["input_digest"],
            code_digest=manifest["code_digest"],
            calibration_id=manifest["calibration_id"],
            as_of=data["as_of"],
        )
        with tenant_session(c.tenant_id) as session:
            row = fenced(session, c)
            result_art = artifact(
                session,
                c.tenant_id,
                "forecast",
                str(row.id),
                result,
                parent_id=uuid.UUID(manifest["input_id"]),
            )
            row.evaluation_due_at = min(
                datetime.combine(
                    date.fromisoformat(v["target_end"]) + timedelta(days=1),
                    time.min,
                    tzinfo=ZoneInfo(r["timezone"]),
                ).astimezone(UTC)
                for r in result["rows"]
                for v in r["horizons"].values()
            )
            row.status = "succeeded"
            row.result_id = result_art.id
            row.lease_owner = None
            row.lease_expires_at = None
        return "succeeded"
    except Exception as exc:
        with tenant_session(c.tenant_id) as session:
            row = session.get(ForecastJob, c.id, with_for_update=True)
            if (
                row
                and row.status == "running"
                and row.lease_owner == c.owner
                and row.lease_expires_at > utcnow()
            ):
                row.status = "failed"
                row.error = str(exc)[:2000]
                row.lease_owner = None
                row.lease_expires_at = None
        return "failed"


def once():
    c = claim()
    return execute(c) if c else score_once()


def sweep():
    with admin_session() as session:
        rows = session.scalars(
            select(ForecastJob)
            .where(
                ForecastJob.status == "running", ForecastJob.lease_expires_at < utcnow()
            )
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            row.status = "queued" if row.attempts < 3 else "failed"
            row.error = "worker lease expired"
            row.lease_owner = None
            row.lease_expires_at = None


def score_once(*, tenant_id=None):
    """Recheck matured labels daily through30 days after the last target ends.

    Claiming advances the due time atomically. Scoring is tenant-serialized and
    append-only, so a retry cannot overwrite labels or inflate promotion support.
    Manual scoring remains available for later source corrections.
    """
    with admin_session() as s:
        row = s.scalar(
            select(ForecastJob)
            .where(
                ForecastJob.status == "succeeded",
                ForecastJob.evaluation_due_at <= utcnow(),
                *([ForecastJob.tenant_id==tenant_id] if tenant_id else []),
            )
            .order_by(ForecastJob.evaluation_due_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        tenant_id, job_id, result_id = row.tenant_id, row.id, row.result_id
        row.evaluation_due_at = utcnow() + timedelta(days=1)
    with tenant_session(tenant_id) as s:
        from .evaluation import evaluate

        evaluate(s, system_principal(tenant_id, "forecast-score-worker"), result_id)
        fc = verified(s, result_id, "forecast")
        last = max(
            date.fromisoformat(v["target_end"])
            for r in fc.payload["rows"]
            for v in r["horizons"].values()
        )
        if utcnow().date() > last + timedelta(days=30):
            s.get(ForecastJob, job_id).evaluation_due_at = None
    return "evaluated"


def capture_due():
    """Retain server snapshots every15 minutes for active ingestion tenants."""
    from ..models import IntegrationConnection, KnowledgeCheckpoint, Tenant

    with admin_session() as s:
        latest = (
            select(func.max(KnowledgeCheckpoint.captured_at))
            .where(KnowledgeCheckpoint.tenant_id == Tenant.id)
            .correlate(Tenant)
            .scalar_subquery()
        )
        active = (
            select(IntegrationConnection.id)
            .where(
                IntegrationConnection.tenant_id == Tenant.id,
                IntegrationConnection.status == "active",
            )
            .exists()
        )
        tenants = list(
            s.scalars(
                select(Tenant.id)
                .where(
                    Tenant.status == "active",
                    active,
                    (latest.is_(None)) | (latest < utcnow() - timedelta(minutes=15)),
                )
                .order_by(latest.asc().nullsfirst())
                .limit(20)
            )
        )
    for tenant_id in tenants:
        with tenant_session(tenant_id) as s:
            lock(s, tenant_id)
            last = s.scalar(select(func.max(KnowledgeCheckpoint.captured_at)))
            if last is None or last < utcnow() - timedelta(minutes=15):
                checkpoint(s, tenant_id)


_schedule_cursor = None


def schedule_forecasts():
    """Bounded keyset scan; issue once per policy/local day after the configured hour.

    Idempotency+tenant locking survive restart and concurrent schedulers. A failed
    source gate is retried on a later scan and never becomes a fabricated issue.
    """
    import logging

    from ..models import PolicyVersion, Tenant

    global _schedule_cursor
    with admin_session() as s:
        query = (
            select(
                PolicyVersion.tenant_id,
                PolicyVersion.id,
                PolicyVersion.config,
                Tenant.timezone,
            )
            .join(Tenant, Tenant.id == PolicyVersion.tenant_id)
            .where(PolicyVersion.is_active, Tenant.status == "active")
        )
        if _schedule_cursor:
            query = query.where(PolicyVersion.tenant_id > _schedule_cursor)
        rows = list(s.execute(query.order_by(PolicyVersion.tenant_id).limit(20)))
        _schedule_cursor = rows[-1][0] if rows else None
    issued = 0
    for tenant_id, policy_id, config, timezone in rows:
        if not config.get("forecast_schedule_enabled"):
            continue
        try:
            local = utcnow().astimezone(ZoneInfo(timezone))
            policy = PlanningPolicy.model_validate(config)
            if local.hour < policy.forecast_schedule_hour_local:
                continue
            key = f"schedule:{policy_id}:{local.date()}"
            with tenant_session(tenant_id) as s:
                lock(s, tenant_id)
                if s.scalar(
                    select(ForecastJob.id).where(ForecastJob.idempotency_key == key)
                ):
                    continue
                control = policy.model_copy(
                    update={
                        "algorithm_version": "aegis-cost-milp-v2",
                        "forecast_result_id": None,
                        "forecast_scenario_id": None,
                        "model_release_id": None,
                    }
                )
                request(
                    s,
                    system_principal(tenant_id, "forecast-scheduler"),
                    key,
                    policy.forecast_schedule_horizons,
                    policy.forecast_schedule_models,
                    control.model_dump(),
                )
                issued += 1
        except (
            Exception
        ):  # Scheduler isolates one tenant's missing source/invalid configuration.
            logging.getLogger("aegis.forecast").exception(
                "scheduled forecast source gate failed",
                extra={"tenant_id": str(tenant_id)},
            )
    return issued
