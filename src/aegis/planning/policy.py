from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    forecast_schedule_enabled: bool = False
    forecast_schedule_hour_local: int = Field(8,ge=0,le=23)
    forecast_schedule_horizons: list[int] = Field(default_factory=lambda:[7,30],min_length=1,max_length=20)
    forecast_schedule_models: list[Literal['rolling-champion-v3','market-aware-v4','seasonal-tsb-v2']] = Field(default_factory=lambda:['rolling-champion-v3'],min_length=1,max_length=3)

    @model_validator(mode='after')
    def forecast_schedule_scope(self):
        if any(type(h)is not int or h<1 or h>365 for h in self.forecast_schedule_horizons):raise ValueError('invalid scheduled forecast horizon')
        if 'market-aware-v4' in self.forecast_schedule_models and not (self.market_signal_source and self.market_signal_platform):raise ValueError('scheduled market model requires an explicit provider and platform')
        return self

    algorithm_version: Literal["aegis-cost-milp-v2", "aegis-protection-v3"] = "aegis-cost-milp-v2"
    forecast_result_id: str | None = None
    forecast_scenario_id: str | None = None
    model_release_id: str | None = None
    protection_quantile: Literal[0.5, 0.8, 0.95] = 0.95

    @model_validator(mode='after')
    def protection_inputs(self):
        import uuid
        if self.algorithm_version == 'aegis-protection-v3' and not self.forecast_result_id:
            raise ValueError('protection-v3 requires an immutable forecast_result_id')
        for value in (self.forecast_result_id, self.forecast_scenario_id, self.model_release_id):
            if value: uuid.UUID(value)
        return self

    # Challenger remains opt-in until tenant-specific outer holdouts pass.
    forecast_model_version: Literal["seasonal-tsb-v2", "rolling-champion-v3", "market-aware-v4"] = "seasonal-tsb-v2"
    market_signal_source: str | None = Field(None, min_length=1, max_length=80)
    market_signal_platform: str | None = Field(None, min_length=1, max_length=80)

    @model_validator(mode='after')
    def market_scope(self):
        if self.forecast_model_version == 'market-aware-v4' and not (self.market_signal_source and self.market_signal_platform):
            raise ValueError('market-aware-v4 requires an explicit market_signal_source and market_signal_platform')
        return self

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
