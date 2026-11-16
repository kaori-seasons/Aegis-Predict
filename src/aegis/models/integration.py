from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, updated_at_col, uuid_pk


def _connection_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connection.id", ondelete="CASCADE"), nullable=False, index=True
    )


class IntegrationConnection(Base):
    __tablename__ = "integration_connection"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint("status in ('active','paused','auth_failed','disabled')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Value of ``source_system`` written on canonical rows produced by this connection.
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    schedule_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lease: exactly one worker may run a connection at a time (fencing for checkpoints).
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SyncStreamState(Base):
    __tablename__ = "sync_stream_state"
    __table_args__ = (
        CheckConstraint("status in ('ok','halted','error')", name="status_valid"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connection.id", ondelete="CASCADE"), primary_key=True
    )
    stream: Mapped[str] = mapped_column(String(60), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    cursor: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_record_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_at_col()


class SyncRun(Base):
    __tablename__ = "sync_run"
    __table_args__ = (
        CheckConstraint("mode in ('incremental','backfill','replay')", name="mode_valid"),
        CheckConstraint("status in ('queued','running','succeeded','partial','failed','cancelled')", name="status_valid"),
        Index("ix_sync_run_queue", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    connection_id: Mapped[uuid.UUID] = _connection_fk()
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="incremental")
    streams: Mapped[list[str] | None] = mapped_column(ARRAY(String(60)))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_by: Mapped[str] = mapped_column(String(320), nullable=False, default="scheduler")
    trace_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_landed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_normalized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RawRecord(Base):
    """Immutable landing zone. Every downstream bug can be repaired by replaying from here."""

    __tablename__ = "raw_record"
    __table_args__ = (
        UniqueConstraint("connection_id", "stream", "external_id", "payload_hash"),
        CheckConstraint(
            "normalize_status in ('pending','ok','quarantined','skipped')", name="normalize_status_valid"
        ),
        Index(
            "ix_raw_record_pending",
            "connection_id",
            "stream",
            "id",
            postgresql_where=text("normalize_status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    connection_id: Mapped[uuid.UUID] = _connection_fk()
    stream: Mapped[str] = mapped_column(String(60), nullable=False)
    external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_ref: Mapped[str | None] = mapped_column(String(500))
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_run.id", ondelete="SET NULL"))
    received_at: Mapped[datetime] = created_at_col()
    normalize_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mapper_version: Mapped[str | None] = mapped_column(String(40))
    # Set by an operator who accepted a record that a quality rule flagged (e.g. a real demand spike).
    quality_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class QuarantineRecord(Base):
    __tablename__ = "quarantine_record"
    __table_args__ = (
        CheckConstraint("status in ('open','resolved','ignored')", name="status_valid"),
        Index("ix_quarantine_open", "tenant_id", "status", "reason_code"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    raw_record_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_record.id", ondelete="CASCADE"), nullable=False, index=True)
    connection_id: Mapped[uuid.UUID] = _connection_fk()
    stream: Mapped[str] = mapped_column(String(60), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = created_at_col()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(320))


class IdMapping(Base):
    """External identifier -> canonical entity (seller_sku/asin/fnsku/erp_sku/wms_code ...)."""

    __tablename__ = "id_mapping"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "entity", "external_id"),
        CheckConstraint("entity in ('sku','location')", name="entity_valid"),
        CheckConstraint("status in ('auto','confirmed','pending')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    entity: Mapped[str] = mapped_column(String(20), nullable=False)
    id_type: Mapped[str] = mapped_column(String(40), nullable=False, default="code")
    external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    resolved_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(60), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class WritebackCommand(Base):
    __tablename__ = "writeback_command"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','sending','sent_unconfirmed','confirmed','dead','cancelled')", name="status_valid"
        ),
        Index("ix_writeback_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    connection_id: Mapped[uuid.UUID] = _connection_fk()
    plan_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("plan_run.id", ondelete="SET NULL"))
    plan_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("plan_line.id", ondelete="SET NULL"))
    command_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_ref: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)
    history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_result"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    connection_id: Mapped[uuid.UUID] = _connection_fk()
    stream: Mapped[str] = mapped_column(String(60), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_count: Mapped[int | None] = mapped_column(Integer)
    local_count: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_ratio: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    backfill_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
