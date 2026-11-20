"""Immutable daily commerce, public context, mapping and launch-event contracts."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, uuid_pk
from .catalog import _location_fk, _sku_fk


class PublishedRevision:
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    raw_record_id: Mapped[int] = mapped_column(
        ForeignKey("raw_record.id"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class CommercePerformanceRevision(PublishedRevision, Base):
    __tablename__ = "commerce_performance_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "channel",
            "metric",
            "metric_date",
            "contract_version",
            "revision",
            name="uq_commerce_revision_identity",
        ),
        CheckConstraint("revision>=1", name="revision_positive"),
        CheckConstraint(
            "state in ('observed','missing','censored')", name="state_valid"
        ),
        CheckConstraint(
            "metric in ('sessions','orders','units','ad_spend','revenue','unit_price')",
            name="metric_valid",
        ),
        CheckConstraint(
            "value IS NULL OR (value>=0 AND value<1e15)", name="value_valid"
        ),
        CheckConstraint(
            "(state='observed' AND value IS NOT NULL) OR state<>'observed'",
            name="observed_value",
        ),
        CheckConstraint(
            "available_at>=received_at AND available_at>=published_at",
            name="availability_valid",
        ),
        Index(
            "ix_commerce_revision_asof",
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "available_at",
            "metric_date",
        ),
    )
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    channel: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(60), nullable=False)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)


class SkuLaunchRevision(PublishedRevision, Base):
    __tablename__ = "sku_launch_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "sku_id",
            "channel",
            "event_kind",
            "revision",
            name="uq_launch_revision_identity",
        ),
        CheckConstraint("revision>=1", name="revision_positive"),
        CheckConstraint(
            "event_kind in ('planned_launch','first_sale')", name="kind_valid"
        ),
        CheckConstraint(
            "available_at>=received_at AND available_at>=published_at",
            name="availability_valid",
        ),
        Index(
            "ix_launch_revision_asof",
            "tenant_id",
            "source_system",
            "sku_id",
            "available_at",
        ),
    )
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    channel: Mapped[str] = mapped_column(String(80), nullable=False)
    launch_group: Mapped[str] = mapped_column(String(120), nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(120), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)


class PublicSignalRevision(PublishedRevision, Base):
    __tablename__ = "public_signal_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "provider",
            "series_key",
            "metric",
            "signal_date",
            "revision",
            name="uq_public_revision_identity",
        ),
        CheckConstraint("revision>=1", name="revision_positive"),
        CheckConstraint("signal_kind in ('observed','planned')", name="kind_valid"),
        CheckConstraint(
            "publication_evidence in ('provider_timestamp','first_seen')",
            name="publication_valid",
        ),
        CheckConstraint(
            "value IS NULL OR (value>=0 AND value<1e15)", name="value_valid"
        ),
        CheckConstraint(
            "available_at>=received_at AND available_at>=published_at",
            name="availability_valid",
        ),
        Index(
            "ix_public_revision_asof",
            "tenant_id",
            "source_system",
            "provider",
            "series_key",
            "available_at",
            "signal_date",
        ),
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    series_key: Mapped[str] = mapped_column(String(240), nullable=False)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    signal_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    publication_evidence: Mapped[str] = mapped_column(String(30), nullable=False)
    attribution: Mapped[str] = mapped_column(String(500), nullable=False)


class PublicSignalBindingRevision(PublishedRevision, Base):
    __tablename__ = "public_signal_binding_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "provider",
            "series_key",
            "metric",
            "revision",
            name="uq_public_binding_identity",
        ),
        CheckConstraint("revision>=1", name="revision_positive"),
        CheckConstraint(
            "available_at>=received_at AND available_at>=published_at",
            name="availability_valid",
        ),
    )
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    series_key: Mapped[str] = mapped_column(String(240), nullable=False)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
