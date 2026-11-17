"""Data quality rules applied between parsing and canonical upsert (plan 5.9).

A failing rule sends the record to the quarantine instead of the canonical tables. Rules in
``OVERRIDABLE`` describe values that can be legitimate (a real promotion spike, a free gift line);
an operator may *accept* such a record, which sets ``raw_record.quality_override`` and replays it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.util import utcnow

OVERRIDABLE = frozenset({"zero_price", "demand_spike"})
LOCATION_KINDS = frozenset({"pool", "warehouse", "fba", "3pl"})

SPIKE_MULTIPLIER = 10.0
SPIKE_LOOKBACK_DAYS = 90
SPIKE_MIN_HISTORY_DAYS = 28
FUTURE_TOLERANCE = timedelta(days=1)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    detail: str

    @property
    def overridable(self) -> bool:
        return self.code in OVERRIDABLE


@dataclass
class QualityChecker:
    """Stateless rules plus a per-batch cache of demand percentiles for spike detection."""

    session: Session
    tenant_id: uuid.UUID
    now: datetime = field(default_factory=utcnow)
    source_system: str | None = None
    _p99_cache: dict[tuple[uuid.UUID, uuid.UUID, date, str], tuple[float, int]] = field(default_factory=dict)

    def check(self, stream: str, record: dict[str, Any], resolved: dict[str, Any] | None = None) -> QualityIssue | None:
        rule = getattr(self, f"_check_{stream}", None)
        return rule(record, resolved or {}) if rule else None

    # -- helpers ---------------------------------------------------------------------------
    def _future(self, value: datetime | date | None, label: str) -> QualityIssue | None:
        if value is None:
            return None
        limit = self.now + FUTURE_TOLERANCE
        if isinstance(value, datetime):
            if value > limit:
                return QualityIssue("future_date", f"{label} {value.isoformat()} is in the future")
        elif value > limit.date():
            return QualityIssue("future_date", f"{label} {value.isoformat()} is in the future")
        return None

    @staticmethod
    def _negative(record: dict[str, Any], fields: tuple[str, ...], code: str) -> QualityIssue | None:
        for name in fields:
            value = record.get(name)
            if value is not None and value < 0:
                return QualityIssue(code, f"{name} is negative ({value})")
        return None

    @staticmethod
    def _unit_interval(record: dict[str, Any], name: str) -> QualityIssue | None:
        value = record.get(name)
        if value is not None and not 0.0 <= value <= 1.0:
            return QualityIssue("out_of_range", f"{name} must be within [0, 1] (got {value})")
        return None

    # -- master data -----------------------------------------------------------------------
    def _check_skus(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if record["pack_size"] <= 0:
            return QualityIssue("invalid_pack_size", f"pack_size must be positive (got {record['pack_size']})")
        return None

    def _check_locations(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if record["kind"] not in LOCATION_KINDS:
            return QualityIssue("invalid_kind", f"kind must be one of {sorted(LOCATION_KINDS)} (got {record['kind']})")
        try:
            ZoneInfo(record["timezone"] or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return QualityIssue("invalid_timezone", f"unknown timezone {record['timezone']!r}")
        return self._unit_interval(record, "last_mile_score") or self._negative(
            record, ("handling_capacity_per_day", "capacity_units", "max_daily_inbound_units"), "negative_value"
        )

    def _check_sku_components(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if record["quantity"] <= 0:
            return QualityIssue("invalid_quantity", f"component quantity must be positive (got {record['quantity']})")
        if record["parent_sku"] == record["component_sku"]:
            return QualityIssue("invalid_reference", "a SKU cannot be its own component")
        return None

    def _check_lanes(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        return self._negative(record, ("lead_time_days", "lead_time_std_days", "unit_cost"), "negative_value")

    def _check_transport_options(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        return self._negative(
            record, ("lead_time_days", "lead_time_std_days", "unit_cost", "max_units"), "negative_value"
        ) or self._unit_interval(record, "reliability")

    def _check_supplier_terms(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        return self._negative(
            record, ("available_units", "min_order_qty", "unit_purchase_cost", "capital_budget"), "negative_value"
        )

    # -- transactional data ----------------------------------------------------------------
    def _check_inventory_snapshots(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        return self._negative(
            record, ("on_hand", "in_transit", "allocated", "backorder"), "negative_inventory"
        ) or self._future(record["as_of"], "as_of")

    def _check_purchase_orders(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        return self._negative(record, ("qty_ordered", "qty_received"), "invalid_quantity")

    def _check_order_lines(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if record["quantity"] <= 0:
            return QualityIssue("invalid_quantity", f"quantity must be positive (got {record['quantity']})")
        issue = self._future(record["ordered_at"], "ordered_at")
        if issue:
            return issue
        price = record.get("unit_price")
        if price is not None and price < 0:
            return QualityIssue("negative_value", f"unit_price is negative ({price})")
        if price is not None and price == 0:
            return QualityIssue("zero_price", "unit_price is 0 (gift, sample or a broken price feed)")
        return None

    def _check_daily_demand(self, record: dict[str, Any], resolved: dict[str, Any]) -> QualityIssue | None:
        if record.get("observation_state") not in (None, "observed", "censored", "missing"):
            return QualityIssue("invalid_observation_state", "state must be observed, censored or missing")
        if record["units"] < 0:
            return QualityIssue("negative_value", f"units is negative ({record['units']})")
        issue = self._future(record["date"], "date")
        if issue:
            return issue
        sku_id, location_id = resolved.get("sku_id"), resolved.get("location_id")
        if sku_id is None or location_id is None or record["units"] == 0:
            return None
        p99, days = self._p99(sku_id, location_id, record["date"], record.get("channel") or "*")
        threshold = SPIKE_MULTIPLIER * max(p99, 1.0)
        if days >= SPIKE_MIN_HISTORY_DAYS and record["units"] > threshold:
            return QualityIssue(
                "demand_spike",
                f"units {record['units']} exceed {SPIKE_MULTIPLIER:g} x P99 ({p99:.1f}) of the last {SPIKE_LOOKBACK_DAYS} days",
            )
        return None

    def _check_market_signals(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if not record['platform'] or len(record['platform']) > 80:
            return QualityIssue('invalid_platform', 'platform must contain 1..80 characters')
        if record['signal_kind'] not in ('planned', 'observed') or record['revision'] < 1:
            return QualityIssue('invalid_market_revision', 'kind must be planned/observed and revision must be positive')
        if record['published_at'] > self.now:
            return QualityIssue('future_publication', 'published_at cannot be in the future; future target dates use planned signals')
        if record['signal_kind'] == 'observed':
            from ..models import Location
            location = self.session.get(Location, _resolved['location_id'])
            end = datetime.combine(record['date']+timedelta(days=1), time.min, ZoneInfo(location.timezone)).astimezone(UTC)
            if record['published_at'] < end:
                return QualityIssue('invalid_observed_publication', 'observed signal must be published after the completed local target day')
        if record['price_index'] <= 0:
            return QualityIssue('invalid_price_index', 'price_index must be positive')
        return self._negative(
            record,
            ("ad_spend_index", "promotion_lift", "seasonal_index", "platform_event_lift", "price_index"),
            "negative_value",
        )

    def _check_fx_rates(self, record: dict[str, Any], _resolved: dict[str, Any]) -> QualityIssue | None:
        if record["rate"] <= 0:
            return QualityIssue("out_of_range", f"rate must be positive (got {record['rate']})")
        return None

    def _p99(self, sku_id: uuid.UUID, location_id: uuid.UUID, day: date, channel: str = "*") -> tuple[float, int]:
        key = (sku_id, location_id, day, channel)
        if key not in self._p99_cache:
            row = self.session.execute(
                text(
                    """
                    SELECT COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY units), 0) AS p99, count(*) AS days
                    FROM demand_observation
                    WHERE tenant_id = :tenant AND sku_id = :sku AND location_id = :location
                      AND source_system = :source AND kind = 'daily_demand' AND channel = :channel
                      AND state = 'observed'
                      AND demand_date >= :start AND demand_date < :day
                    """
                ),
                {
                    "tenant": self.tenant_id,
                    "source": self.source_system,
                    "channel": channel,
                    "sku": sku_id,
                    "location": location_id,
                    "start": day - timedelta(days=SPIKE_LOOKBACK_DAYS),
                    "day": day,
                },
            ).one()
            self._p99_cache[key] = (float(row.p99), int(row.days))
        return self._p99_cache[key]
