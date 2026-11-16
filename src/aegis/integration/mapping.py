from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..core.util import utcnow
from ..models import IdMapping, Location, Sku


class MappingIssue(Exception):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


@dataclass
class MappingContext:
    """Resolves external SKU / location identifiers to canonical ids for one tenant + source system.

    Resolution order: confirmed/auto ``id_mapping`` row for the source system, then exact canonical
    code match. Unknown identifiers are registered as ``pending`` mappings so operators can resolve
    them in the UI and replay the quarantined records.
    """

    tenant_id: uuid.UUID
    source_system: str
    naive_timezone: str = "UTC"
    platform: str = "generic"
    skus: dict[str, uuid.UUID] = field(default_factory=dict)
    locations: dict[str, uuid.UUID] = field(default_factory=dict)
    location_codes: dict[uuid.UUID, str] = field(default_factory=dict)
    location_timezones: dict[uuid.UUID, str] = field(default_factory=dict)
    location_kinds: dict[uuid.UUID, str] = field(default_factory=dict)
    sku_mappings: dict[str, uuid.UUID | None] = field(default_factory=dict)
    location_mappings: dict[str, uuid.UUID | None] = field(default_factory=dict)
    _pending: set[tuple[str, str]] = field(default_factory=set)
    _auto_resolved: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session, tenant_id: uuid.UUID, source_system: str, naive_timezone: str = "UTC") -> "MappingContext":
        ctx = cls(tenant_id=tenant_id, source_system=source_system, naive_timezone=naive_timezone)
        ctx.refresh(session)
        return ctx

    def refresh(self, session: Session) -> None:
        self.skus = {code: sku_id for code, sku_id in session.execute(select(Sku.code, Sku.id))}
        self.locations = {}
        for location_id, code, timezone, kind in session.execute(
            select(Location.id, Location.code, Location.timezone, Location.kind)
        ):
            self.locations[code] = location_id
            self.location_codes[location_id] = code
            self.location_timezones[location_id] = timezone
            self.location_kinds[location_id] = kind
        self.sku_mappings = {}
        self.location_mappings = {}
        for entity, external_id, target_id, status in session.execute(
            select(IdMapping.entity, IdMapping.external_id, IdMapping.target_id, IdMapping.status).where(
                IdMapping.source_system == self.source_system
            )
        ):
            resolved = target_id if status in ("auto", "confirmed") else None
            (self.sku_mappings if entity == "sku" else self.location_mappings)[external_id] = resolved

    def sku_id(self, external: str) -> uuid.UUID:
        return self._resolve("sku", external, self.sku_mappings, self.skus, "unmapped_sku", "SKU")

    def location_id(self, external: str) -> uuid.UUID:
        return self._resolve("location", external, self.location_mappings, self.locations, "unmapped_location", "location")

    def _resolve(
        self,
        entity: str,
        external: str,
        mappings: dict[str, uuid.UUID | None],
        canonical: dict[str, uuid.UUID],
        reason: str,
        label: str,
    ) -> uuid.UUID:
        resolved = mappings.get(external)
        if resolved is not None:
            return resolved
        if external in canonical:
            if external in mappings:
                # A pending mapping whose code now exists canonically resolves itself.
                mappings[external] = canonical[external]
                self._auto_resolved[(entity, external)] = canonical[external]
            return canonical[external]
        if external in mappings:
            raise MappingIssue(reason, f"{label} '{external}' has a pending mapping for source {self.source_system}")
        self._pending.add((entity, external))
        raise MappingIssue(reason, f"{label} '{external}' is unknown for source {self.source_system}")

    def flush_pending(self, session: Session) -> None:
        """Register unknown identifiers as pending mappings and auto-resolve the ones that now match."""

        for (entity, external), target in sorted(self._auto_resolved.items()):
            session.execute(
                update(IdMapping)
                .where(
                    IdMapping.tenant_id == self.tenant_id,
                    IdMapping.source_system == self.source_system,
                    IdMapping.entity == entity,
                    IdMapping.external_id == external,
                    IdMapping.status == "pending",
                )
                .values(target_id=target, status="auto", confidence=1.0, resolved_by="system:code-match", updated_at=utcnow())
            )
        self._auto_resolved.clear()
        if not self._pending:
            return
        rows = [
            {
                "id": uuid.uuid4(),
                "tenant_id": self.tenant_id,
                "source_system": self.source_system,
                "entity": entity,
                "id_type": "code",
                "external_id": external,
                "status": "pending",
                "confidence": 0.0,
            }
            for entity, external in sorted(self._pending)
        ]
        session.execute(
            insert(IdMapping)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tenant_id", "source_system", "entity", "external_id"])
        )
        self._pending.clear()


def resolve_mapping(
    session: Session,
    mapping: IdMapping,
    *,
    target_code: str,
    resolved_by: str,
) -> uuid.UUID:
    """Point an external identifier at a canonical entity and confirm the mapping."""

    if mapping.entity == "sku":
        target = session.scalar(select(Sku.id).where(Sku.code == target_code, Sku.is_active))
    else:
        target = session.scalar(select(Location.id).where(Location.code == target_code, Location.is_active))
    if target is None:
        from ..core.errors import InvalidInput

        raise InvalidInput(f"{mapping.entity} '{target_code}' does not exist")
    session.execute(
        update(IdMapping)
        .where(IdMapping.id == mapping.id)
        .values(target_id=target, status="confirmed", confidence=1.0, resolved_by=resolved_by, updated_at=utcnow())
    )
    return target
