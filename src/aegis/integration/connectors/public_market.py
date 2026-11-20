"""Bounded no-key public adapters. Immutable first-seen snapshots, never fake vintages."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import httpx

from ...core.util import content_hash, utcnow
from ..errors import ContractError, TransientError
from ..retry import RetryPolicy, call_with_retry
from ..spi import Checkpoint, HealthResult, RawItem
from .generic_rest import _retry_after

USER_AGENT = "Aegis-Predict/0.12 (+https://github.com/kaori-seasons/Aegis-Predict)"
PROVIDERS = {
    "frankfurter": {
        "name": "Frankfurter FX",
        "endpoint": "https://api.frankfurter.dev/v2/rates",
        "license": "Free blended reference rates; provider-specific endpoints have separate terms",
        "attribution": "Frankfurter blended reference rates — https://frankfurter.dev/",
        "metric": "fx_rate",
        "unit": "quote_per_base",
        "kind": "observed",
    },
    "wikimedia": {
        "name": "Wikimedia Pageviews",
        "endpoint": "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/",
        "license": "CC0 1.0; User-Agent and respectful sequential requests required",
        "attribution": "Wikimedia Analytics API, CC0 1.0 — https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/",
        "metric": "pageviews",
        "unit": "count",
        "kind": "observed",
    },
    "govuk_holidays": {
        "name": "GOV.UK Bank Holidays",
        "endpoint": "https://www.gov.uk/bank-holidays.json",
        "license": "Open Government Licence v3.0; UK divisions only",
        "attribution": "Contains public sector information licensed under the Open Government Licence v3.0 — https://www.gov.uk/bank-holidays.json",
        "metric": "public_holiday",
        "unit": "indicator",
        "kind": "planned",
    },
}
DIVISIONS = ("england-and-wales", "scotland", "northern-ireland")


def validate(config):
    allowed = {"subscriptions", "cache_ttl_seconds", "history_days"}
    if set(config) - allowed:
        raise ValueError(
            "only registered public endpoints are supported; arbitrary URLs/credentials are forbidden"
        )
    subscriptions = config.get("subscriptions")
    if not isinstance(subscriptions, list) or not 1 <= len(subscriptions) <= 12:
        raise ValueError("choose1–12 public subscriptions")
    result = []
    seen = set()
    for s in subscriptions:
        if not isinstance(s, dict) or set(s) - {"provider", "series_key"}:
            raise ValueError("subscription requires provider and series_key only")
        provider = s.get("provider")
        series = s.get("series_key")
        if provider not in PROVIDERS or not isinstance(series, str):
            raise ValueError("unknown provider/series")
        if provider == "frankfurter":
            series = series.upper()
            if (
                not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", series)
                or series[:3] == series[4:]
            ):
                raise ValueError("FX series is BASE/QUOTE, e.g. USD/EUR")
        elif provider == "wikimedia":
            bits = series.split("|", 1)
            if (
                len(bits) != 2
                or not re.fullmatch(
                    r"[a-z][a-z0-9-]{1,20}\.(wikipedia|wiktionary)\.org", bits[0]
                )
                or not bits[1]
                or len(bits[1]) > 160
                or any(ord(c) < 32 for c in bits[1])
            ):
                raise ValueError(
                    "pageview series is project|Article, e.g. en.wikipedia.org|USB-C"
                )
        elif series not in DIVISIONS:
            raise ValueError(
                "GOV.UK covers england-and-wales, scotland and northern-ireland only"
            )
        if (provider, series) in seen:
            raise ValueError("duplicate public subscription")
        seen.add((provider, series))
        result.append({"provider": provider, "series_key": series})
    ttl = config.get("cache_ttl_seconds", 3600)
    days = config.get("history_days", 90)
    if type(ttl) is not int or not 300 <= ttl <= 86400:
        raise ValueError("cache TTL must be300–86400 seconds")
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("public history is bounded to365days")
    return {"subscriptions": result, "cache_ttl_seconds": ttl, "history_days": days}


def request_json(client, ctx, url, params=None):
    def attempt():
        ctx.rate_limiter.acquire("public-market")
        try:
            with client.stream(
                "GET",
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as r:
                if r.status_code == 429 or r.status_code >= 500:
                    delay = _retry_after(r)
                    ctx.rate_limiter.penalize("public-market", delay or 1)
                    raise TransientError(
                        f"public source HTTP {r.status_code}", retry_after=delay
                    )
                if r.status_code != 200:
                    raise ContractError(
                        f"public source HTTP {r.status_code}; no empty-success fallback"
                    )
                chunks = []
                size = 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ContractError("public source exceeds2MB response budget")
                    chunks.append(chunk)
                try:
                    return json.loads(b"".join(chunks))
                except (ValueError, UnicodeDecodeError) as e:
                    raise ContractError("public source returned invalid JSON") from e
        except httpx.TransportError as e:
            raise TransientError(f"public transport: {type(e).__name__}") from e

    return call_with_retry(attempt, RetryPolicy(max_attempts=3), sleep=ctx.sleep)


def parse_response(subscription, data, start, end):
    provider = subscription["provider"]
    series = subscription["series_key"]
    spec = PROVIDERS[provider]
    rows = []

    def add(day, value):
        if not start <= day <= end:
            return
        if type(value) not in (int, float) or not 0 <= value < 1e15:
            raise ContractError("invalid public numeric value")
        rows.append(
            {
                "provider": provider,
                "series_key": series,
                "metric": spec["metric"],
                "date": str(day),
                "value": value,
                "unit": spec["unit"],
                "signal_kind": spec["kind"],
                "publication_evidence": "first_seen",
                "attribution": spec["attribution"],
                "revision": 0,
                "is_deleted": False,
            }
        )

    try:
        if provider == "frankfurter":
            if not isinstance(data, list) or not data:
                raise ValueError("empty FX coverage")
            base, target = series.split("/")
            for r in data:
                if r["base"].upper() != base or r["quote"].upper() != target:
                    raise ValueError("unexpected currency scope")
                if r["rate"] <= 0:
                    raise ValueError("nonpositive FX")
                add(date.fromisoformat(r["date"]), r["rate"])
        elif provider == "wikimedia":
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("items"), list)
                or not data["items"]
            ):
                raise ValueError("empty pageview coverage")
            project, article = series.split("|", 1)
            for r in data["items"]:
                if (
                    r["project"] not in (project, project.removesuffix(".org"))
                    or r["article"].replace(" ", "_") != article.replace(" ", "_")
                    or r["agent"] != "user"
                    or r["access"] != "all-access"
                    or r["granularity"] != "daily"
                ):
                    raise ValueError("unexpected pageview scope")
                if type(r["views"]) is not int:
                    raise ValueError("pageviews require whole counts")
                add(datetime.strptime(r["timestamp"], "%Y%m%d%H").date(), r["views"])
        else:
            if not isinstance(data, dict) or not isinstance(data.get(series), dict):
                raise ValueError("missing UK division")
            division = data[series]
            if (
                division.get("division") != series
                or not isinstance(division.get("events"), list)
                or not division["events"]
            ):
                raise ValueError("incomplete UK calendar")
            events = division["events"]
            days = {
                date.fromisoformat(r["date"])
                for r in events
                if isinstance(r.get("title"), str)
            }
            if len(days) != len(events):
                raise ValueError("duplicate/incomplete holiday events")
            # A provider year is only considered covered when that year actually
            # occurs in the complete official calendar. Unsupported years remain missing.
            years = {d.year for d in days}
            for i in range((end - start).days + 1):
                day = start + timedelta(days=i)
                if day.year in years:
                    add(day, int(day in days))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ContractError(f"{provider}: invalid scoped response") from e
    if not rows:
        raise ContractError(f"{provider}: no rows in the requested covered interval")
    identities = [r["date"] for r in rows]
    if len(identities) != len(set(identities)):
        raise ContractError("duplicate public daily observations")
    return sorted(rows, key=lambda r: r["date"])


def mock_response(subscription, start, end):
    provider = subscription["provider"]
    series = subscription["series_key"]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if provider == "frankfurter":
        base, quote_currency = series.split("/")
        return [
            {
                "date": str(d),
                "base": base,
                "quote": quote_currency,
                "rate": 0.9 + (i % 14) * 0.001,
            }
            for i, d in enumerate(days)
            if d.weekday() < 5
        ]
    if provider == "wikimedia":
        project, article = series.split("|", 1)
        return {
            "items": [
                {
                    "project": project.removesuffix(".org"),
                    "article": article.replace(" ", "_"),
                    "agent": "user",
                    "access": "all-access",
                    "granularity": "daily",
                    "timestamp": d.strftime("%Y%m%d00"),
                    "views": 1000 + (i % 14) * 100,
                }
                for i, d in enumerate(days)
            ]
        }
    events = [
        {
            "date": str(date(y, 1, 1)),
            "title": "Mock New Year",
            "notes": "Synthetic fixture",
            "bunting": True,
        }
        for y in sorted({d.year for d in days})
    ]
    return {
        division: {"division": division, "events": events} for division in DIVISIONS
    }


class PublicMarketConnector:
    kind = "public_market"
    version = "official-public-context-v1"

    def validate_config(self, config, settings=None):
        return validate(config)

    def streams(self, config):
        return ["public_signals"]

    def check(self, ctx):
        return HealthResult(
            True,
            "fixed no-key public providers; sync validates network/schema",
            {"providers": ctx.config["subscriptions"], "is_mock": False},
        )

    def count(self, ctx, stream, window):
        return None

    def read(self, ctx, stream, cursor, window):
        if stream != "public_signals":
            raise ValueError("unsupported public stream")
        clock = (ctx.now or utcnow)()
        config = ctx.config
        fingerprint = content_hash(config)
        if (
            cursor
            and not window
            and cursor.get("config_hash") == fingerprint
            and (clock - datetime.fromisoformat(cursor["fetched_at"])).total_seconds()
            < config["cache_ttl_seconds"]
        ):
            yield Checkpoint(stream, cursor)
            return
        end = clock.astimezone(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=config["history_days"] - 1)
        if window:
            start = max(start, window.start.date())
            end = min(end, window.end.date() - timedelta(days=1))
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            cache = {}
            for subscription in config["subscriptions"]:
                provider = subscription["provider"]
                series = subscription["series_key"]
                last = end
                if provider == "govuk_holidays":
                    last = clock.date() + timedelta(days=365)
                if provider == "frankfurter":
                    base, target = series.split("/")
                    url = PROVIDERS[provider]["endpoint"]
                    params = {
                        "base": base,
                        "quotes": target,
                        "from": str(start),
                        "to": str(last),
                    }
                elif provider == "wikimedia":
                    project, article = series.split("|", 1)
                    url = (
                        PROVIDERS[provider]["endpoint"]
                        + f"{project}/all-access/user/{quote(article.replace(' ', '_'), safe='')}/daily/{start:%Y%m%d}00/{last:%Y%m%d}00"
                    )
                    params = None
                else:
                    url = PROVIDERS[provider]["endpoint"]
                    params = None
                key = (url, json.dumps(params, sort_keys=True))
                if key not in cache:
                    cache[key] = (
                        mock_response(subscription, start, last)
                        if self.kind.startswith("mock_")
                        else request_json(client, ctx, url, params)
                    )
                for row in parse_response(subscription, cache[key], start, last):
                    yield RawItem(
                        stream,
                        content_hash([provider, series, row["date"], row["value"]]),
                        row,
                        source_ref=url,
                    )
        yield Checkpoint(
            stream, {"fetched_at": clock.isoformat(), "config_hash": fingerprint}
        )


class MockPublicMarketConnector(PublicMarketConnector):
    kind = "mock_public_market"

    def check(self, ctx):
        return HealthResult(
            True, "offline official-shape public fixtures", {"is_mock": True}
        )
