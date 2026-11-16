"""Tenant-safe ingestion evidence, source ownership and inventory reservations."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, created_at_col, tenant_fk, uuid_pk


class IngestionBatch(Base):
    __tablename__ = "ingestion_batch"
    __table_args__ = (
        CheckConstraint("status in ('landing','landed','complete','quarantined','failed')", name="status_valid"),
        Index("ix_ingestion_batch_stream", "tenant_id", "connection_id", "stream", "created_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_connection.id"), nullable=False)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_run.id"))
    stream: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="landing")
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    digest: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    landed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchRecord(Base):
    __tablename__ = "ingestion_batch_record"
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_batch.id"), primary_key=True)
    raw_record_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("raw_record.id"), primary_key=True)


class SourceAuthority(Base):
    __tablename__ = "source_authority"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", "location_id", "channel"),
        CheckConstraint("domain in ('demand','inventory','inbound')", name="domain_valid"),
        CheckConstraint("stream in ('order_lines','daily_demand','inventory_snapshots','purchase_orders')", name="stream_valid"),
        CheckConstraint("inbound_mode in ('linked','disjoint')", name="inbound_mode_valid"),
        CheckConstraint("domain = 'demand' OR channel = '*'", name="channel_valid"),
        CheckConstraint("(domain='demand' AND stream in ('order_lines','daily_demand')) OR (domain='inventory' AND stream='inventory_snapshots') OR (domain='inbound' AND stream='purchase_orders')", name="stream_domain"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    domain: Mapped[str] = mapped_column(String(20), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(80), nullable=False, default="*")
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_connection.id"), nullable=False)
    stream: Mapped[str] = mapped_column(String(60), nullable=False)
    # Explicit declaration: whether PO outstanding and snapshot transit are disjoint.
    inbound_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="linked")
    coverage_start: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class DemandObservation(Base):
    __tablename__ = "demand_observation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "kind", "sku_id", "location_id", "channel", "demand_date"),
        CheckConstraint("units >= 0", name="units_nonnegative"),
        CheckConstraint("state in ('observed','censored','missing')", name="state_valid"),
        CheckConstraint("kind in ('order_lines','daily_demand')", name="kind_valid"),
        Index("ix_demand_observation_lookup", "tenant_id", "location_id", "demand_date"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    sku_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sku.id"), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(80), nullable=False, default="*")
    demand_date: Mapped[date] = mapped_column(Date, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="observed")
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = created_at_col()


class InventoryInboundLink(Base):
    __tablename__ = "inventory_inbound_link"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "purchase_order_line_id"),
        CheckConstraint("units > 0", name="units_positive"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_snapshot.id"), nullable=False)
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("purchase_order_line.id"), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class InventoryReservation(Base):
    __tablename__ = "inventory_reservation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_line_id"),
        CheckConstraint("units > 0", name="units_positive"),
        CheckConstraint("status in ('held','released','consumed')", name="status_valid"),
        CheckConstraint("consumed_units >= 0 AND released_units >= 0 AND settled_units >= 0 AND settled_units <= consumed_units AND consumed_units + released_units <= units", name="counters_valid"),
        Index("ix_inventory_reservation_stock", "tenant_id", "sku_id", "location_id", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sku.id"), nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_snapshot.id"), nullable=False)
    plan_line_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plan_line.id"), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="held")
    created_at: Mapped[datetime] = created_at_col()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_evidence: Mapped[str | None] = mapped_column(Text)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    released_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    settled_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
