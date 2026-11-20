"""Canonical stream templates.

Every connector emits records using these field names (``file_csv`` via column headers,
``generic_rest`` via ``field_map``). Parsing is strict: a value that cannot be converted
is a *data error* (quarantine), a missing column in a file header is a *contract error*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class FieldError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str  # str | int | float | bool | date | datetime | decimal
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class CanonicalStream:
    name: str
    kind: str  # master | event | snapshot | timeseries
    key_fields: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    order: int

    @property
    def field_names(self) -> list[str]:
        return [spec.name for spec in self.fields]

    @property
    def required_fields(self) -> list[str]:
        return [spec.name for spec in self.fields if spec.required]

    def external_id(self, record: dict[str, Any]) -> str:
        parts = []
        for key in self.key_fields:
            value = record.get(key)
            if value is None or str(value).strip() == "":
                raise FieldError(key, "key field is empty")
            parts.append(str(value).strip())
        return "|".join(parts)


def _f(name: str, type_: str, required: bool = False, default: Any = None) -> FieldSpec:
    return FieldSpec(name, type_, required, default)


UPDATED_AT = _f("updated_at", "datetime")

STREAMS: dict[str, CanonicalStream] = {
    stream.name: stream
    for stream in [
        CanonicalStream(
            "skus",
            "master",
            ("sku",),
            (
                _f("sku", "str", True),
                _f("name", "str", True),
                _f("category", "str", default=""),
                _f("pack_size", "int", True),
                _f("unit_margin", "float", True),
                _f("service_class", "str", default="standard"),
                _f("is_active", "bool", default=True),
                UPDATED_AT,
            ),
            10,
        ),
        CanonicalStream(
            "locations",
            "master",
            ("code",),
            (
                _f("code", "str", True),
                _f("name", "str", default=""),
                _f("kind", "str", True),
                _f("region", "str", default=""),
                _f("country", "str", default=""),
                _f("timezone", "str", default="UTC"),
                _f("handling_capacity_per_day", "int", default=0),
                _f("last_mile_score", "float", default=0.0),
                _f("latitude", "float", default=0.0),
                _f("longitude", "float", default=0.0),
                _f("capacity_units", "int"),
                _f("storage_cost_per_unit_day", "float"),
                _f("max_daily_inbound_units", "int"),
                _f("is_active", "bool", default=True),
                UPDATED_AT,
            ),
            20,
        ),
        CanonicalStream(
            "sku_components",
            "master",
            ("parent_sku", "component_sku"),
            (_f("parent_sku", "str", True), _f("component_sku", "str", True), _f("quantity", "int", True), UPDATED_AT),
            30,
        ),
        CanonicalStream(
            "lanes",
            "master",
            ("from", "to"),
            (
                _f("from", "str", True),
                _f("to", "str", True),
                _f("lead_time_days", "int", True),
                _f("lead_time_std_days", "float"),
                _f("unit_cost", "float", True),
                UPDATED_AT,
            ),
            40,
        ),
        CanonicalStream(
            "transport_options",
            "master",
            ("from", "to", "mode"),
            (
                _f("from", "str", True),
                _f("to", "str", True),
                _f("mode", "str", True),
                _f("lead_time_days", "int", True),
                _f("lead_time_std_days", "float"),
                _f("unit_cost", "float", True),
                _f("reliability", "float", True),
                _f("max_units", "int", True),
                UPDATED_AT,
            ),
            50,
        ),
        CanonicalStream(
            "supplier_terms",
            "master",
            ("sku", "supplier"),
            (
                _f("sku", "str", True),
                _f("supplier", "str", True),
                _f("available_units", "int", True),
                _f("min_order_qty", "int", default=0),
                _f("unit_purchase_cost", "float", True),
                _f("capital_budget", "float", True),
                _f("currency", "str", default="USD"),
                UPDATED_AT,
            ),
            60,
        ),
        CanonicalStream(
            "inventory_snapshots",
            "snapshot",
            ("sku", "location", "as_of"),
            (
                _f("sku", "str", True),
                _f("location", "str", True),
                _f("as_of", "datetime", True),
                _f("on_hand", "int", True),
                _f("in_transit", "int", default=0),
                _f("allocated", "int", default=0),
                _f("backorder", "int", default=0),
            ),
            70,
        ),
        CanonicalStream(
            "purchase_orders",
            "event",
            ("po_id", "line_id"),
            (
                _f("po_id", "str", True),
                _f("line_id", "str", True),
                _f("sku", "str", True),
                _f("location", "str", True),
                _f("qty_ordered", "int", True),
                _f("qty_received", "int", default=0),
                _f("status", "str", True),
                _f("expected_arrival", "date"),
                _f("supplier", "str", default=""),
                UPDATED_AT,
            ),
            80,
        ),
        CanonicalStream(
            "order_lines",
            "event",
            ("order_id", "line_id"),
            (
                _f("order_id", "str", True),
                _f("line_id", "str", True),
                _f("sku", "str", True),
                _f("location", "str", True),
                _f("quantity", "int", True),
                _f("status", "str", True),
                _f("ordered_at", "datetime", True),
                _f("unit_price", "decimal"),
                _f("currency", "str"),
                _f("channel", "str", default=""),
                UPDATED_AT,
            ),
            90,
        ),
        CanonicalStream(
            "daily_demand",
            "timeseries",
            ("sku", "location", "date"),
            (
                _f("sku", "str", True),
                _f("location", "str", True),
                _f("date", "date", True),
                _f("units", "int", True),
                _f("stockout", "bool", default=False),
                _f("channel", "str", default="*"),
                _f("observation_state", "str"),
                UPDATED_AT,
            ),
            100,
        ),
        CanonicalStream(
            "market_signals",
            "timeseries",
            ("sku", "location", "platform", "signal_kind", "date", "revision"),
            (
                _f("sku", "str", True),
                _f("location", "str", True),
                _f("date", "date", True),
                _f("platform", "str", True),
                _f("signal_kind", "str", True),
                _f("revision", "int", True),
                _f("published_at", "aware_datetime", True),
                _f("is_deleted", "bool", default=False),
                _f("ad_spend_index", "float", default=1.0),
                _f("promotion_lift", "float", default=1.0),
                _f("seasonal_index", "float", default=1.0),
                _f("platform_event_lift", "float", default=1.0),
                _f("price_index", "float", default=1.0),
                UPDATED_AT,
            ),
            110,
        ),
        CanonicalStream(
            "fx_rates",
            "timeseries",
            ("base", "quote", "date"),
            (_f("base", "str", True), _f("quote", "str", True), _f("date", "date", True), _f("rate", "decimal", True)),
            120,
        ),
    ]
}

# Separate metric schemas: sessions and launch facts are never squeezed into index columns.
_REVISION_FIELDS=(_f('revision','int',default=1),_f('published_at','aware_datetime'),_f('is_deleted','bool',default=False))
_NEW_STREAMS=[
 CanonicalStream('commerce_performance','timeseries',('sku','location','channel','metric','date','contract_version','revision'),
  (_f('sku','str',True),_f('location','str',True),_f('channel','str',True),_f('metric','str',True),_f('date','date',True),
   _f('contract_version','str',default='commerce-daily-v1'),_f('timezone','str',True),_f('unit','str',True),_f('currency','str',default=''),
   _f('state','str',default='observed'),_f('value','float'),*_REVISION_FIELDS),130),
 CanonicalStream('sku_launches','event',('sku','channel','event_kind','revision'),
  (_f('sku','str',True),_f('channel','str',default='*'),_f('launch_group','str',True),_f('cohort_key','str',True),
   _f('event_kind','str',True),_f('date','date',True),_f('note','str',True),*_REVISION_FIELDS),125),
 CanonicalStream('public_signals','timeseries',('provider','series_key','metric','date'),
  (_f('provider','str',True),_f('series_key','str',True),_f('metric','str',True),_f('date','date',True),_f('value','float'),
   _f('unit','str',True),_f('signal_kind','str',True),_f('publication_evidence','str',default='first_seen'),_f('attribution','str',True),
   _f('revision','int',default=0),_f('published_at','aware_datetime'),_f('is_deleted','bool',default=False)),135),
 CanonicalStream('public_signal_bindings','event',('sku','location','provider','series_key','metric','revision'),
  (_f('sku','str',True),_f('location','str',True),_f('provider','str',True),_f('series_key','str',True),_f('metric','str',True),
   _f('effective_from','date',True),_f('note','str',True),*_REVISION_FIELDS),140),
]
STREAMS.update({stream.name:stream for stream in _NEW_STREAMS})


MASTER_STREAMS = frozenset(name for name, stream in STREAMS.items() if stream.kind == "master")


def ordered_streams(names: list[str] | set[str]) -> list[str]:
    unknown = sorted(set(names) - set(STREAMS))
    if unknown:
        raise ValueError(f"unknown streams: {', '.join(unknown)}")
    return sorted(set(names), key=lambda name: STREAMS[name].order)


_TRUE = {"true", "1", "yes", "y", "t", "是"}
_FALSE = {"false", "0", "no", "n", "f", "否", ""}


def parse_datetime(value: Any, naive_tz: str = "UTC") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(naive_tz))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {naive_tz}") from exc
    return parsed.astimezone(UTC)


def _convert(spec: FieldSpec, value: Any, naive_tz: str) -> Any:
    if spec.type == "str":
        return str(value).strip()
    if spec.type == "int":
        if isinstance(value, bool):
            raise ValueError("boolean is not an integer")
        if isinstance(value, int):
            if not -(2**31) <= value < 2**31:
                raise ValueError("integer exceeds supported quantity range")
            return value
        number = Decimal(str(value).strip().replace(",", ""))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError(f"{value!r} is not an integer")
        if not -(2**31) <= number < 2**31:
            raise ValueError("integer exceeds supported quantity range")
        return int(number)
    if spec.type == "float":
        number = float(str(value).strip().replace(",", ""))
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("not a finite number")
        return number
    if spec.type == "decimal":
        number = Decimal(str(value).strip().replace(",", ""))
        if not number.is_finite():
            raise ValueError("not a finite decimal")
        return number
    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{value!r} is not a boolean")
    if spec.type == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])
    if spec.type == "aware_datetime":
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('explicit timezone offset is required')
        return parsed.astimezone(UTC)
    if spec.type == "datetime":
        return parse_datetime(value, naive_tz)
    raise ValueError(f"unsupported field type {spec.type}")


def parse_record(stream: CanonicalStream, payload: dict[str, Any], naive_tz: str = "UTC") -> dict[str, Any]:
    """Typed canonical record. Raises ``FieldError`` (-> quarantine) on the first invalid field."""

    record: dict[str, Any] = {}
    for spec in stream.fields:
        raw = payload.get(spec.name)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            if spec.required:
                raise FieldError(spec.name, "required field is missing")
            record[spec.name] = spec.default
            continue
        try:
            record[spec.name] = _convert(spec, raw, naive_tz)
            limits = {"sku": 80, "parent_sku": 80, "component_sku": 80, "code": 64, "location": 64,
                      "from": 64, "to": 64, "order_id": 120, "line_id": 120, "po_id": 120,
                      "name": 200 if stream.name == "locations" else 300, "mode": 20, "status": 40,
                      "timezone": 64, "channel": 80, "category": 120, "supplier": 200, "platform": 200,
                      "currency": 3, "base": 3, "quote": 3, "region": 80, "country": 8}
            if spec.type == "str" and len(record[spec.name]) > limits.get(spec.name, 1000):
                raise ValueError("field exceeds canonical length; identifiers are never truncated")
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise FieldError(spec.name, f"invalid {spec.type}: {exc}") from exc
    return record
