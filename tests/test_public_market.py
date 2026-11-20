from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest

from aegis.integration.connectors.public_market import (
    MockPublicMarketConnector,
    mock_response,
    parse_response,
    request_json,
    validate,
)
from aegis.integration.errors import ContractError
from aegis.integration.ratelimit import RateLimiter
from aegis.integration.spi import Checkpoint, RawItem

START = date(2026, 1, 1)
END = date(2026, 1, 10)
SUBSCRIPTIONS = [
    {"provider": "frankfurter", "series_key": "USD/EUR"},
    {"provider": "wikimedia", "series_key": "en.wikipedia.org|USB-C"},
    {"provider": "govuk_holidays", "series_key": "england-and-wales"},
    {"provider": "govuk_holidays", "series_key": "scotland"},
]


def context():
    return SimpleNamespace(
        config=validate({"subscriptions": SUBSCRIPTIONS, "history_days": 10}),
        now=lambda: datetime(2026, 1, 11, tzinfo=UTC),
        rate_limiter=RateLimiter(1000, 1000),
        sleep=lambda _: None,
    )


def test_official_shape_fixtures_scoped_cache_and_cursor():
    c = MockPublicMarketConnector()
    ctx = context()
    events = list(c.read(ctx, "public_signals", None, None))
    rows = [r for r in events if isinstance(r, RawItem)]
    assert {r.payload["provider"] for r in rows} == {
        "frankfurter",
        "wikimedia",
        "govuk_holidays",
    }
    assert len({r.payload["series_key"] for r in rows}) == 4
    assert all(
        "published_at" not in r.payload
        and r.payload["publication_evidence"] == "first_seen"
        for r in rows
    )
    assert isinstance(events[-1], Checkpoint)
    assert list(c.read(ctx, "public_signals", events[-1].cursor, None)) == [events[-1]]
    ctx.config["subscriptions"] = SUBSCRIPTIONS[:1]
    assert len(list(c.read(ctx, "public_signals", events[-1].cursor, None))) > 1


def test_provider_scope_and_no_unsupported_calendar_zeros():
    data = mock_response(SUBSCRIPTIONS[0], START, END)
    data[0]["quote"] = "GBP"
    with pytest.raises(ContractError):
        parse_response(SUBSCRIPTIONS[0], data, START, END)
    data = mock_response(SUBSCRIPTIONS[1], START, END)
    data["items"][0]["agent"] = "spider"
    with pytest.raises(ContractError):
        parse_response(SUBSCRIPTIONS[1], data, START, END)
    data = mock_response(SUBSCRIPTIONS[2], START, END)
    with pytest.raises(ContractError):
        parse_response(SUBSCRIPTIONS[2], data, date(2040, 1, 1), date(2040, 1, 5))
    with pytest.raises(ValueError):
        validate({"subscriptions": SUBSCRIPTIONS, "base_url": "http://localhost"})
    with pytest.raises(ValueError):
        validate(
            {
                "subscriptions": [
                    {"provider": "wikimedia", "series_key": "internal.local|x"}
                ]
            }
        )


def test_retry_user_agent_response_budget_and_no_false_success():
    attempts = []
    delays = []
    ctx = context()
    ctx.sleep = delays.append

    def handler(req):
        assert "Aegis-Predict" in req.headers["user-agent"]
        attempts.append(req)
        return (
            httpx.Response(429, headers={"Retry-After": "1"})
            if len(attempts) == 1
            else httpx.Response(200, json=[])
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert request_json(client, ctx, "https://api.frankfurter.dev/v2/rates") == []
    assert len(attempts) == 2 and delays == [1]
    for response in (
        httpx.Response(404),
        httpx.Response(200, content=b"x" * 2_000_001),
        httpx.Response(200, content=b"not json"),
    ):
        with httpx.Client(
            transport=httpx.MockTransport(lambda req: response)
        ) as client:
            with pytest.raises(ContractError):
                request_json(client, ctx, "https://api.frankfurter.dev/v2/rates")
