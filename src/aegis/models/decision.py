"""Point-in-time evidence and independent decision operations.

Artifacts are immutable, content addressed, typed by server-owned schemas. Mutable
workflow records only hold approval/assignment state; they never replace evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, updated_at_col, uuid_pk


class FactRevision(Base):
    __tablename__ = "fact_revision"
    __table_args__ = (
        Index("ix_fact_revision_replay", "tenant_id", "table_name", "entity_key", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    table_name: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(30), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class KnowledgeCheckpoint(Base):
    __tablename__ = "knowledge_checkpoint"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    pg_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class DecisionArtifact(Base):
    __tablename__ = "decision_artifact"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "identity"),
        CheckConstraint(
            "kind in ('input','forecast','actual','evaluation','calibration','bom','scenario','promotion_evidence')",
            name="kind_valid",
        ),
        Index("ix_decision_artifact_kind", "tenant_id", "kind", "created_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    identity: Mapped[str] = mapped_column(String(160), nullable=False)
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_checkpoint.id")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_artifact.id")
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("pg_current_xact_id()::text")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[datetime] = created_at_col()


class ForecastJob(Base):
    __tablename__ = "forecast_job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="status_valid",
        ),
        Index("ix_forecast_job_claim", "status", "created_at"),
        Index("ix_forecast_job_evaluate", "status", "evaluation_due_at"),
        Index(
            "ix_forecast_job_scope", "tenant_id", "scope_key", "kernel_digest", "status"
        ),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    scope_key: Mapped[str | None] = mapped_column(String(64))
    kernel_digest: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_artifact.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    horizons: Mapped[list] = mapped_column(JSONB, nullable=False)
    model_versions: Mapped[list] = mapped_column(JSONB, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_artifact.id")
    )
    evaluation_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DecisionCase(Base):
    """CAS guarded drafts, model release pointers and inventory risk state machines."""

    __tablename__ = "decision_case"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "scope_key"),
        CheckConstraint(
            "kind in ('bom_draft','override','model_release','risk')", name="kind_valid"
        ),
        Index("ix_decision_case_kind", "tenant_id", "kind", "updated_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_artifact.id")
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
