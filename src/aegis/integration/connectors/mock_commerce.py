"""Canonical daily operating and launch fixtures. Never fake system receipt history."""

from datetime import UTC, date, datetime, time, timedelta

from ...core.util import content_hash, utcnow
from ..spi import Checkpoint, HealthResult, RawItem


class MockCommerceConnector:
    kind = "mock_commerce"
    version = "commerce-daily-v1"

    def validate_config(self, config, settings=None):
        allowed = {
            "sku",
            "location_code",
            "channel",
            "timezone",
            "start_date",
            "days",
            "launch_group",
            "cohort_key",
            "currency",
            "launch_date",
        }
        if set(config) - allowed:
            raise ValueError("unknown commerce fixture setting")
        start = date.fromisoformat(str(config.get("start_date", "2026-01-01")))
        days = config.get("days", 180)
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("fixture days must be1–365")
        result = {
            k: str(config.get(k, default))
            for k, default in [
                ("sku", "S"),
                ("location_code", "W"),
                ("channel", "amazon"),
                ("timezone", "UTC"),
                ("launch_group", "mock-product-1"),
                ("cohort_key", "accessories"),
                ("currency", "USD"),
            ]
        }
        if any(not v.strip() or len(v) > 120 for v in result.values()):
            raise ValueError("invalid fixture dimensions")
        from zoneinfo import ZoneInfo

        ZoneInfo(result["timezone"])
        result.update(
            start_date=str(start),
            days=days,
            launch_date=str(date.fromisoformat(str(config.get("launch_date", start)))),
        )
        return result

    def streams(self, config):
        return ["sku_launches", "commerce_performance"]

    def check(self, ctx):
        return HealthResult(
            True, "offline explicit operating metric fixture", {"is_mock": True}
        )

    def count(self, ctx, stream, window):
        return None

    def read(self, ctx, stream, cursor, window):
        c = ctx.config
        clock = (ctx.now or utcnow)()
        start = date.fromisoformat(c["start_date"])
        if stream == "sku_launches":
            day = date.fromisoformat(c["launch_date"])
            pub = datetime.combine(day + timedelta(days=1), time(9), UTC)
            if pub <= clock:
                row = {
                    "sku": c["sku"],
                    "channel": c["channel"],
                    "launch_group": c["launch_group"],
                    "cohort_key": c["cohort_key"],
                    "event_kind": "first_sale",
                    "date": str(day),
                    "note": "Mock confirmed first-sale event",
                    "revision": 1,
                    "published_at": pub.isoformat(),
                }
                yield RawItem(stream, content_hash(row), row, pub)
        elif stream == "commerce_performance":
            from zoneinfo import ZoneInfo

            for i in range(c["days"]):
                day = start + timedelta(days=i)
                pub = datetime.combine(
                    day + timedelta(days=1), time(9), ZoneInfo(c["timezone"])
                ).astimezone(UTC)
                if pub > clock:
                    continue
                if window and not window.start <= pub < window.end:
                    continue
                sessions = 100 + (i // 7 % 4) * 50
                orders = sessions // 10
                for metric, value in [
                    ("sessions", sessions),
                    ("orders", orders),
                    ("units", orders * 2),
                    ("ad_spend", sessions * 0.05),
                    ("unit_price", 10.0),
                    ("revenue", orders * 20.0),
                ]:
                    money = metric in ("ad_spend", "unit_price", "revenue")
                    row = {
                        "sku": c["sku"],
                        "location": c["location_code"],
                        "channel": c["channel"],
                        "date": str(day),
                        "metric": metric,
                        "value": value,
                        "unit": "money" if money else "count",
                        "currency": c["currency"] if money else "",
                        "state": "observed",
                        "timezone": c["timezone"],
                        "contract_version": "commerce-daily-v1",
                        "revision": 1,
                        "published_at": pub.isoformat(),
                    }
                    yield RawItem(
                        stream, content_hash([c["sku"], str(day), metric]), row, pub
                    )
        else:
            raise ValueError("unsupported commerce fixture stream")
        yield Checkpoint(stream, {"published_through": clock.isoformat()})
