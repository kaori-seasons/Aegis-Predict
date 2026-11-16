from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at_col, tenant_fk, updated_at_col, uuid_pk


def _sku_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("sku.id", ondelete="RESTRICT"), nullable=False)


def _location_fk(nullable: bool = False) -> Mapped:
    return mapped_column(UUID(as_uuid=True), ForeignKey("location.id", ondelete="RESTRICT"), nullable=nullable)


class SourceTracked:
    """Columns shared by every canonical row written through the integration pipeline."""

    source_system: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Sku(SourceTracked, Base):
    __tablename__ = "sku"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        CheckConstraint("pack_size > 0", name="pack_size_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    service_class: Mapped[str] = mapped_column(String(40), nullable=False, default="standard")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SkuComponent(Base):
    """Bundle / multipack explosion: one unit of ``parent`` consumes ``quantity`` of ``component``."""

    __tablename__ = "sku_component"
    __table_args__ = (UniqueConstraint("tenant_id", "parent_sku_id", "component_sku_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    parent_sku_id: Mapped[uuid.UUID] = _sku_fk()
    component_sku_id: Mapped[uuid.UUID] = _sku_fk()
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Location(SourceTracked, Base):
    """Pool (central stock), owned overseas warehouse, FBA or third party warehouse."""

    __tablename__ = "location"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        CheckConstraint("kind in ('pool','warehouse','fba','3pl')", name="kind_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="warehouse")
    region: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    handling_capacity_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_mile_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latitude: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    longitude: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    capacity_units: Mapped[int | None] = mapped_column(Integer)
    storage_cost_per_unit_day: Mapped[float | None] = mapped_column(Float)
    max_daily_inbound_units: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Lane(SourceTracked, Base):
    """Default replenishment lane between two locations (``lead_time_std_days`` feeds safety stock)."""

    __tablename__ = "lane"
    __table_args__ = (UniqueConstraint("tenant_id", "from_location_id", "to_location_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    from_location_id: Mapped[uuid.UUID] = _location_fk()
    to_location_id: Mapped[uuid.UUID] = _location_fk()
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    lead_time_std_days: Mapped[float | None] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class TransportOption(SourceTracked, Base):
    __tablename__ = "transport_option"
    __table_args__ = (UniqueConstraint("tenant_id", "from_location_id", "to_location_id", "mode"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    from_location_id: Mapped[uuid.UUID] = _location_fk()
    to_location_id: Mapped[uuid.UUID] = _location_fk()
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    lead_time_std_days: Mapped[float | None] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    max_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SupplierTerms(SourceTracked, Base):
    __tablename__ = "supplier_terms"
    __table_args__ = (UniqueConstraint("tenant_id", "sku_id", "supplier"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    supplier: Mapped[str] = mapped_column(String(200), nullable=False)
    available_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_purchase_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    capital_budget: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")


class InventorySnapshot(Base):
    """Point-in-time stock. ``as_of`` is business time, ``ingested_at`` is load time."""

    __tablename__ = "inventory_snapshot"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_id", "location_id", "as_of", "source_system"),
        Index("ix_inventory_snapshot_latest", "tenant_id", "sku_id", "location_id", "as_of"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_transit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backorder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_system: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ingested_at: Mapped[datetime] = created_at_col()
    ingestion_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_batch.id"))


class OrderLine(SourceTracked, Base):
    __tablename__ = "order_line"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_order_id", "external_line_id"),
        Index("ix_order_line_demand", "tenant_id", "sku_id", "location_id", "ordered_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    external_order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    external_line_id: Mapped[str] = mapped_column(String(120), nullable=False)
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    channel: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    demand_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))


class DailyDemand(Base):
    """Daily units per SKU x location in the location's local calendar. ``stockout`` marks censored days."""

    __tablename__ = "daily_demand"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), primary_key=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sku.id"), primary_key=True)
    location_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("location.id"), primary_key=True)
    demand_date: Mapped[date] = mapped_column(Date, primary_key=True)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stockout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="import")
    source_system: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    updated_at: Mapped[datetime] = updated_at_col()


class PurchaseOrderLine(SourceTracked, Base):
    """Open purchase / transfer orders feed the inventory position (on order)."""

    __tablename__ = "purchase_order_line"
    __table_args__ = (UniqueConstraint("tenant_id", "source_system", "external_po_id", "external_line_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    external_po_id: Mapped[str] = mapped_column(String(120), nullable=False)
    external_line_id: Mapped[str] = mapped_column(String(120), nullable=False)
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    supplier: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    qty_ordered: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_arrival: Mapped[date | None] = mapped_column(Date)


class MarketSignal(SourceTracked, Base):
    __tablename__ = "market_signal"
    __table_args__ = (UniqueConstraint("tenant_id", "sku_id", "location_id", "signal_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    sku_id: Mapped[uuid.UUID] = _sku_fk()
    location_id: Mapped[uuid.UUID] = _location_fk()
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    platform: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    ad_spend_index: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    promotion_lift: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    seasonal_index: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    platform_event_lift: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    price_index: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class FxRate(SourceTracked, Base):
    __tablename__ = "fx_rate"
    __table_args__ = (UniqueConstraint("tenant_id", "base_currency", "quote_currency", "rate_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
