from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False, index=True)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


# Tables that carry tenant_id but are intentionally outside RLS. ``user_session`` is looked up
# by token hash before the tenant is known; the hash is the only lookup key.
RLS_EXEMPT_TABLES = frozenset({"user_session"})
# Append-only tables: the application role only receives SELECT/INSERT.
APPEND_ONLY_TABLES = frozenset({"audit_log", "source_reconciliation", "execution_observation", "inventory_settlement", "market_signal_revision", "fact_revision", "knowledge_checkpoint", "decision_artifact", "commerce_performance_revision", "sku_launch_revision", "public_signal_revision", "public_signal_binding_revision", "launch_profile_revision", "expert_response"})


def tenant_tables() -> list[str]:
    return sorted(
        table.name
        for table in Base.metadata.sorted_tables
        if "tenant_id" in table.columns and table.name not in RLS_EXEMPT_TABLES
    )
