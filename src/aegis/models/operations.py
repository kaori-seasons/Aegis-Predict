"""Identity bindings and immutable reconciliation / physical execution evidence."""
from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, created_at_col, tenant_fk, uuid_pk


class OAuthAttempt(Base):
    __tablename__ = "oauth_attempt"
    __table_args__ = (CheckConstraint("status in ('pending','exchanging','succeeded','failed')", name="status_valid"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ExternalIdentity(Base):
    __tablename__ = "external_identity"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "subject"), UniqueConstraint("tenant_id", "provider", "user_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="google")
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class SourceReconciliation(Base):
    __tablename__ = "source_reconciliation"
    __table_args__ = (CheckConstraint("status in ('matched','mismatch')", name="status_valid"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_batch.id"), nullable=False, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_connection.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    local_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checked_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ExecutionObservation(Base):
    __tablename__ = "execution_observation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reservation_id", "source_version"),
        CheckConstraint("shipped >= 0 AND received >= 0 AND cancelled >= 0 AND received <= shipped", name="quantities_valid"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_reservation.id"), nullable=False, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("integration_connection.id"), nullable=False)
    source_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    shipped: Mapped[int] = mapped_column(Integer, nullable=False)
    received: Mapped[int] = mapped_column(Integer, nullable=False)
    cancelled: Mapped[int] = mapped_column(Integer, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class InventorySettlement(Base):
    __tablename__ = "inventory_settlement"
    __table_args__ = (UniqueConstraint("tenant_id", "observation_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("inventory_reservation.id"), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("execution_observation.id"), nullable=False)
    source_check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_reconciliation.id"), nullable=False)
    destination_check_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("source_reconciliation.id"))
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ReconciliationCase(Base):
    """Operational follow-up; never substitutes for immutable reconciliation evidence."""
    __tablename__ = 'reconciliation_case'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'batch_id'),
        CheckConstraint("status in ('open','in_progress','resolved')", name='status_valid'),
        CheckConstraint('revision >= 1', name='revision_valid'),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('ingestion_batch.id'), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='open')
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('app_user.id'))
    resolution_check_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('source_reconciliation.id'))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = created_at_col()
