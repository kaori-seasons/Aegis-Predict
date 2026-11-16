from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    review_period_days: int = Field(7, ge=1, le=90)
    history_days: int = Field(180, ge=28, le=730)
    service_level_z: dict[str, float] = Field(default_factory=lambda: {"standard": 1.28, "premium": 1.65})
    stockout_penalty: float = Field(20.0, gt=0)
    holding_cost_per_unit_day: float = Field(0.02, ge=0)
    duty_per_unit: float = Field(0.0, ge=0)
    last_mile_per_unit: float = Field(0.0, ge=0)
    approval_value_threshold: float = Field(1200.0, ge=0)
    approval_urgency_threshold: float = Field(0.55, ge=0, le=1)
    min_confidence: float = Field(0.6, ge=0, le=1)
    solver_time_limit_seconds: float = Field(30, gt=0, le=120)
    solver_mip_gap: float = Field(0.02, ge=0, le=0.5)
    max_inventory_age_hours: float = Field(2, gt=0)
    max_demand_age_days: int = Field(2, ge=1, le=30)
    max_quarantine_ratio: float = Field(0.01, ge=0, le=1)
    max_missing_demand_ratio: float = Field(0.05, ge=0, le=1)
    allow_stale_data: bool = False
    # Fail closed on ambiguous inventory ownership and PO/transit double counting.
    inventory_source: str | None = None
    demand_source: str | None = None
    po_source: str | None = None
    po_includes_transit: bool = True
