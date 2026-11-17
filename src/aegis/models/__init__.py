"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from .base import APPEND_ONLY_TABLES, RLS_EXEMPT_TABLES, Base, tenant_tables
from .catalog import (
    DailyDemand,
    FxRate,
    InventorySnapshot,
    Lane,
    Location,
    MarketSignal,
    OrderLine,
    PurchaseOrderLine,
    Sku,
    SkuComponent,
    SupplierTerms,
    TransportOption,
)
from .identity import AppUser, AuditLog, Tenant, UserSession
from .integration import (
    IdMapping,
    IntegrationConnection,
    OutboxEvent,
    QuarantineRecord,
    RawRecord,
    ReconciliationResult,
    SyncRun,
    SyncStreamState,
    WritebackCommand,
)
from .planning import PlanInputBlob, PlanLine, PlanLinePin, PlanRun, PolicyVersion
from .foundation import (BatchRecord, DemandObservation, IngestionBatch,
                         InventoryInboundLink, InventoryReservation, SourceAuthority)

from .operations import ReconciliationCase, OAuthAttempt, ExternalIdentity, SourceReconciliation, ExecutionObservation, InventorySettlement
from .market import MarketSignalRevision
from .decision import FactRevision, KnowledgeCheckpoint, DecisionArtifact, ForecastJob, DecisionCase

__all__ = [
    "APPEND_ONLY_TABLES",
    "AppUser",
    "AuditLog",
    "Base",
    "DailyDemand",
    "FxRate",
    "IdMapping",
    "IntegrationConnection",
    "InventorySnapshot",
    "Lane",
    "Location",
    "MarketSignal",
    "MarketSignalRevision",
    "OrderLine",
    "OutboxEvent",
    "PlanInputBlob",
    "PlanLine",
    "PlanLinePin",
    "PlanRun",
    "PolicyVersion",
    "PurchaseOrderLine",
    "QuarantineRecord",
    "RLS_EXEMPT_TABLES",
    "RawRecord",
    "ReconciliationResult",
    "Sku",
    "SkuComponent",
    "SupplierTerms",
    "SyncRun",
    "SyncStreamState",
    "Tenant",
    "TransportOption",
    "UserSession",
    "WritebackCommand",
    "tenant_tables",
]


def _production_constraints() -> None:
    """Mirror migration 0002 invariants in metadata for drift detection/autogeneration."""
    from sqlalchemy import ForeignKeyConstraint, Index, UniqueConstraint, text
    tables = list(Base.metadata.sorted_tables)
    references = []
    for table in tables:
        if "tenant_id" not in table.c:
            continue
        if "id" in table.c:
            table.append_constraint(UniqueConstraint("tenant_id", "id", name=f"uq_{table.name}_tenant_ref"))
        for fk in list(table.foreign_keys):
            if len(fk.constraint.elements) == 1 and fk.column.name == "id" and "tenant_id" in fk.column.table.c:
                references.append((table, fk.parent.name, fk.column.table.name))
    for table, column, parent in references:
        table.append_constraint(ForeignKeyConstraint(["tenant_id", column], [f"{parent}.tenant_id", f"{parent}.id"],
                                                     name=f"fk_{table.name}_{column}_tenant"))
    Index("uq_plan_run_active", PlanRun.tenant_id, unique=True,
          postgresql_where=text("status in ('queued','solving','pending_review','released')"))
    IntegrationConnection.__table__.append_constraint(UniqueConstraint("tenant_id", "source_system", name="uq_connection_source"))


_production_constraints()
