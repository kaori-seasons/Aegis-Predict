"""Explicit no-network market fixture. Ingestion still assigns today's availability.

Historical dates here describe provider publication, never fabricated system receipt.
Offline backtest fixtures simulate availability separately and identify themselves as such.
"""

from datetime import UTC, date, datetime, time, timedelta
from ...core.util import utcnow
from ..spi import Checkpoint, HealthResult, RawItem

FIXTURE_VERSION = "market-publication-v1"


def market_fixture(start, days, sku="S", location="W", platform="amazon"):
    start = date.fromisoformat(str(start))
    for i in range(days):
        day = start + timedelta(days=i)
        published = datetime.combine(day - timedelta(days=7), time(12), UTC)
        body = {
            "sku": sku,
            "location": location,
            "date": day.isoformat(),
            "platform": platform,
            "signal_kind": "planned",
            "revision": 1,
            "published_at": published.isoformat(),
            "ad_spend_index": 1.5 if i % 14 < 4 else 1.0,
            "promotion_lift": 1.8 if i % 14 < 4 else 1.0,
            "seasonal_index": 1.0,
            "platform_event_lift": 1.0,
            "price_index": 0.9 if i % 14 < 4 else 1.0,
        }
        yield body
        # A later correction must not rewrite what earlier origins knew.
        yield body | {
            "revision": 2,
            "published_at": (published + timedelta(days=10)).isoformat(),
            "promotion_lift": 1.1,
        }


class MockMarketConnector:
    kind = "mock_market"
    version = FIXTURE_VERSION

    def validate_config(self, config, settings=None):
        start = date.fromisoformat(str(config.get("start_date", "2026-01-01")))
        days = config.get("days", 180)
        if type(days) is not int or not 1 <= days <= 400:
            raise ValueError("days must be an integer in [1,400]")
        result = {"start_date": start.isoformat(), "days": days}
        for key, default, limit in [
            ("sku", "S", 80),
            ("location_code", "W", 64),
            ("platform", "amazon", 80),
        ]:
            value = config.get(key, default)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("invalid " + key)
            result[key] = value.strip()
        return result

    def streams(self, config):
        return ["market_signals"]

    def check(self, ctx):
        return HealthResult(
            True,
            "offline market fixture",
            {"fixture_version": self.version, "is_mock": True},
        )

    def count(self, ctx, stream, window):
        return None

    def read(self, ctx, stream, cursor, window):
        if stream != "market_signals":
            raise ValueError("unsupported market fixture stream")
        clock = (ctx.now or utcnow)()
        previous = (
            datetime.fromisoformat(cursor["published_through"]) if cursor else None
        )
        config = ctx.config
        for row in market_fixture(
            config["start_date"],
            config["days"],
            config["sku"],
            config["location_code"],
            config["platform"],
        ):
            published = datetime.fromisoformat(row["published_at"])
            if published > clock or (previous and published <= previous):
                continue
            if window and not window.start <= published < window.end:
                continue
            key = "|".join(
                str(row[k])
                for k in (
                    "sku",
                    "location",
                    "platform",
                    "signal_kind",
                    "date",
                    "revision",
                )
            )
            yield RawItem(
                "market_signals", key, row, published, source_ref=self.version
            )
        yield Checkpoint(stream, {"published_through": clock.isoformat()})
