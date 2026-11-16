from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, updated_at_col, uuid_pk

PLAN_STATUSES = ("queued", "solving", "pending_review", "released", "closed", "discarded", "failed", "cancelled")
LINE_STATUSES = ("proposed", "approved", "rejected", "released", "writeback_failed", "confirmed")


class PolicyVersion(Base):
    """Immutable, versioned planning policy (service levels, thresholds, solver budget, data gates)."""

    __tablename__ = "policy_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "version"),
        Index("uq_policy_version_active", "tenant_id", unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class PlanRun(Base):
    __tablename__ = "plan_run"
    __table_args__ = (
        CheckConstraint("status in ('" + "','".join(PLAN_STATUSES) + "')", name="status_valid"),
        CheckConstraint("mode in ('shadow','execution')", name="mode_valid"),
        # agentic-aps style "pending plan": at most one plan awaiting review per tenant.
        Index(
            "uq_plan_run_pending_review",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'pending_review'"),
        ),
        Index("ix_plan_run_queue", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="shadow")
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("policy_version.id"), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    writeback_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connection.id", ondelete="SET NULL")
    )
    input_digest: Mapped[str | None] = mapped_column(String(64))
    data_watermarks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    freshness: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    stale_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_acknowledged_by: Mapped[str | None] = mapped_column(String(320))
    model_name: Mapped[str | None] = mapped_column(String(300))
    algorithm_version: Mapped[str | None] = mapped_column(String(40))
    optimizer_status: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str] = mapped_column(String(320), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(320))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_col()


class PlanInputBlob(Base):
    """Frozen solver input (gzip JSON in ``sample_network.json`` format) for exact reproduction."""

    __tablename__ = "plan_input_blob"

    plan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_run.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class PlanLine(Base):
    __tablename__ = "plan_line"
    __table_args__ = (
        UniqueConstraint("plan_run_id", "sku_id", "location_id"),
        CheckConstraint("status in ('" + "','".join(LINE_STATUSES) + "')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    plan_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plan_run.id", ondelete="CASCADE"), nullable=False, index=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sku.id"), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), nullable=False)
    sku_code: Mapped[str] = mapped_column(String(80), nullable=False)
    location_code: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    inventory_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommended_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocated_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unmet_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transport_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urgency: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decision_by: Mapped[str | None] = mapped_column(String(320))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class PlanLinePin(Base):
    """Manual override kept across re-plans (agentic-aps ``pinned``): fixed units / transport mode."""

    __tablename__ = "plan_line_pin"
    __table_args__ = (
        Index(
            "uq_plan_line_pin_active",
            "tenant_id",
            "sku_id",
            "location_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sku.id"), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    transport_mode: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    source_plan_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
