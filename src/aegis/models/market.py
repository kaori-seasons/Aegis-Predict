"""Immutable provider revisions; availability is assigned by the ingestion service."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, created_at_col, tenant_fk, uuid_pk
from .catalog import _sku_fk, _location_fk


class MarketSignalRevision(Base):
    __tablename__ = "market_signal_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "platform",
            "signal_kind",
            "signal_date",
            "revision",
            name="uq_market_revision_identity",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("signal_kind in ('planned','observed')", name="kind_valid"),
        CheckConstraint(
            "available_at >= published_at AND available_at >= received_at",
            name="availability_valid",
        ),
        CheckConstraint(
            "ad_spend_index >= 0 AND promotion_lift >= 0 AND seasonal_index >= 0 AND platform_event_lift >= 0 AND price_index > 0",
            name="indices_valid",
        ),
        Index(
            "ix_market_revision_asof",
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "platform",
            "available_at",
            "signal_date",
        ),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    raw_record_id: Mapped[int] = mapped_column(
        ForeignKey("raw_record.id"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    platform: Mapped[str] = mapped_column(String(80), nullable=False)
    signal_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
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
    ad_spend_index: Mapped[float] = mapped_column(Float, nullable=False)
    promotion_lift: Mapped[float] = mapped_column(Float, nullable=False)
    seasonal_index: Mapped[float] = mapped_column(Float, nullable=False)
    platform_event_lift: Mapped[float] = mapped_column(Float, nullable=False)
    price_index: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
