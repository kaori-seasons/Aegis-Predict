"""Launch reference facts and immutable, private expert responses."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, uuid_pk
from .catalog import _location_fk, _sku_fk
from .commerce import PublishedRevision


class LaunchProfileRevision(PublishedRevision, Base):
    __tablename__ = "launch_profile_revision"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "channel",
            "revision",
            name="uq_launch_profile_identity",
        ),
        CheckConstraint("revision>=1", name="revision_positive"),
        CheckConstraint(
            "retail_price IS NULL OR (retail_price>=0 AND retail_price<1e12)",
            name="price_valid",
        ),
        CheckConstraint(
            "initial_supply_units IS NULL OR initial_supply_units BETWEEN 0 AND 10000000",
            name="initial_supply_valid",
        ),
        CheckConstraint(
            "first_batch_scope IN ('unknown','warehouse_channel','sku_launch')",
            name="batch_scope_valid",
        ),
        CheckConstraint(
            "price_basis IN ('unknown','tax_inclusive','tax_exclusive')",
            name="price_basis_valid",
        ),
        CheckConstraint(
            "available_at>=received_at AND available_at>=published_at",
            name="availability_valid",
        ),
        Index(
            "ix_launch_profile_asof",
            "tenant_id",
            "source_system",
            "sku_id",
            "location_id",
            "available_at",
        ),
    )
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    channel: Mapped[str] = mapped_column(String(80), nullable=False)
    positioning: Mapped[str] = mapped_column(Text, nullable=False)
    pricing_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    retail_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    price_basis: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )
    launch_plan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    initial_supply_units: Mapped[int | None] = mapped_column(Integer)
    first_batch_scope: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown"
    )


class ExpertResponse(Base):
    __tablename__ = "expert_response"
    __table_args__ = (
        UniqueConstraint("tenant_id", "study_id", "round_number", "user_id"),
        CheckConstraint("round_number BETWEEN 1 AND 5", name="round_valid"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decision_case.id"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
