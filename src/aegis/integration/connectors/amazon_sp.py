"""Amazon SP-API: Orders 2026-01-01 + FBA Inventory v1, one marketplace per connection.

No PII datasets are requested. Only an allowlisted planning projection is landed. Inventory
is a complete polling snapshot: Amazon does not report all inbound changes incrementally.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..markets import AMAZON_MARKETS
from ...core.util import utcnow
from ..errors import AuthError, ContractError
from ..spi import Checkpoint, HealthResult, RawItem
from ..streams import parse_datetime
from .platform_http import send_json

HOSTS = {"NA": "https://sellingpartnerapi-na.amazon.com", "EU": "https://sellingpartnerapi-eu.amazon.com", "FE": "https://sellingpartnerapi-fe.amazon.com"}


class AmazonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: Literal["NA", "EU", "FE"]
    marketplace_id: str = Field(min_length=1, max_length=50)
    seller_id: str = Field(min_length=1, max_length=40)
    location_code: str = Field(min_length=1, max_length=64)
    initial_since: str
    lookback_minutes: int = Field(1440, ge=120, le=10080)
    max_pages: int = Field(500, ge=1, le=10000)
    streams: list[Literal["order_lines", "inventory_snapshots"]] = Field(default_factory=lambda: ["order_lines", "inventory_snapshots"])
    # Conservative searchOrders quota; inventory uses the same rate unless overridden later.
    rate_limit: dict = Field(default_factory=lambda: {"rate_per_second": 0.0056, "burst": 20})

    @model_validator(mode="after")
    def market_region(self):
        profile = next((p for p in AMAZON_MARKETS.values() if p[0] == self.marketplace_id), None)
        if profile is None or profile[1] != self.region:
            raise ValueError("marketplace_id must match a known market and its SP-API region")
        return self


class AmazonSpConnector:
    kind = "amazon_sp"
    version = "2026-01-01"

    def validate_config(self, config, settings=None):
        parsed = AmazonConfig.model_validate(config)
        parse_datetime(parsed.initial_since, "UTC")
        return parsed.model_dump(mode="json")

    def streams(self, config):
        return config["streams"]

    def count(self, ctx, stream, window):
        return None

    def _client(self):
        return httpx.Client(timeout=30, follow_redirects=False, trust_env=False)

    def _token(self, client, ctx):
        now = (ctx.now or utcnow)()
        if ctx.secrets.get("_expires_at") and now.timestamp() < ctx.secrets["_expires_at"]:
            return ctx.secrets["_access_token"]
        required = ("client_id", "client_secret", "refresh_token")
        if not all(ctx.secrets.get(k) for k in required):
            raise AuthError("Amazon client_id, client_secret and refresh_token are required")
        document = send_json(client, ctx, "POST", "https://api.amazon.com/auth/o2/token", endpoint="amazon:token",
                             data={"grant_type": "refresh_token", **{k: ctx.secrets[k] for k in required}})
        if not document.get("access_token"):
            raise AuthError("Amazon LWA returned no access token")
        ctx.secrets["_access_token"] = document["access_token"]
        ctx.secrets["_expires_at"] = now.timestamp() + max(1, int(document.get("expires_in", 3600)) - 60)
        return document["access_token"]

    def _get(self, client, ctx, config, path, params):
        if path == "/fba/inventory/v1/summaries":
            ctx.rate_limiter.configure(f"amazon:{path}", 2.0, 2.0)
        for attempt in range(2):
            try:
                return send_json(client, ctx, "GET", HOSTS[config.region] + path, endpoint=f"amazon:{path}",
                                 params=params, headers={"x-amz-access-token": self._token(client, ctx)})
            except AuthError:
                ctx.secrets.pop("_expires_at", None)
                if attempt:
                    raise

    def check(self, ctx):
        try:
            config = AmazonConfig.model_validate(ctx.config)
            with self._client() as client:
                document = self._get(client, ctx, config, "/sellers/v1/marketplaceParticipations", {})
            participations = document.get("payload")
            if not isinstance(participations, list):
                raise ContractError("Amazon participation response is incomplete")
            accepted = any(p.get("marketplace", {}).get("id") == config.marketplace_id
                           and p.get("participation", {}).get("isParticipating") is True for p in participations)
            return HealthResult(accepted, "marketplace participation checked; seller identity requires OAuth callback verification")
        except (AuthError, ContractError) as exc:
            return HealthResult(False, str(exc))

    def read(self, ctx, stream, cursor, window):
        config = AmazonConfig.model_validate(ctx.config)
        now = (ctx.now or utcnow)()
        until = window.end if window else now - timedelta(minutes=2)
        since = window.start if window else parse_datetime((cursor or {}).get("updated_since") or config.initial_since, "UTC") - timedelta(minutes=config.lookback_minutes)
        if since >= until and stream == "order_lines":
            return
        snapshot_at = now.isoformat()
        if stream == "inventory_snapshots":
            params = {"granularityType": "Marketplace", "granularityId": config.marketplace_id,
                      "marketplaceIds": config.marketplace_id, "details": "true"}
            path = "/fba/inventory/v1/summaries"
        elif stream == "order_lines":
            params = {"marketplaceIds": config.marketplace_id, "lastUpdatedAfter": since.isoformat(),
                      "lastUpdatedBefore": until.isoformat(), "maxResultsPerPage": 100, "includedData": "FULFILLMENT"}
            path = "/orders/2026-01-01/orders"
        else:
            raise ContractError("unsupported Amazon stream")
        seen = set()
        with self._client() as client:
            for _ in range(config.max_pages):
                document = self._get(client, ctx, config, path, params)
                if stream == "order_lines":
                    orders = document.get("orders")
                    if not isinstance(orders, list):
                        raise ContractError("Amazon response missing orders")
                    for order in orders:
                        updated = parse_datetime(order["lastUpdatedTime"], "UTC")
                        status = order.get("fulfillment", {}).get("fulfillmentStatus")
                        if not status:
                            raise ContractError("Amazon fulfillment status missing")
                        for item in order["orderItems"]:
                            payload = {"order_id": order["orderId"], "line_id": item["orderItemId"],
                                       "sku": item["product"].get("sellerSku"), "location": config.location_code,
                                       "quantity": item["quantityOrdered"], "status": status.lower(),
                                       "ordered_at": order["createdTime"], "channel": f"amazon:{config.seller_id}:{config.marketplace_id}"}
                            yield RawItem(stream, f'{order["orderId"]}|{item["orderItemId"]}', payload, updated)
                    token = (document.get("pagination") or {}).get("nextToken")
                    token_key = "paginationToken"
                else:
                    summaries = (document.get("payload") or {}).get("inventorySummaries")
                    if not isinstance(summaries, list):
                        raise ContractError("Amazon response missing inventorySummaries")
                    for item in summaries:
                        details = item.get("inventoryDetails")
                        if not isinstance(details, dict):
                            raise ContractError("Amazon inventory details missing")
                        # Sellable units only; reserved and unsellable quantities are excluded by definition.
                        # Do not deduct reserved units a second time from fulfillableQuantity.
                        payload = {"sku": item["sellerSku"], "location": config.location_code, "as_of": snapshot_at,
                                   "on_hand": details.get("fulfillableQuantity", 0), "allocated": 0,
                                   "in_transit": sum(details.get(k, 0) for k in ("inboundWorkingQuantity", "inboundShippedQuantity", "inboundReceivingQuantity")),
                                   "backorder": 0}
                        yield RawItem(stream, f'{item["sellerSku"]}|{config.location_code}|{snapshot_at}', payload, now)
                    token = (document.get("pagination") or {}).get("nextToken")
                    token_key = "nextToken"
                if not token:
                    break
                if token in seen:
                    raise ContractError("Amazon pagination token repeated")
                seen.add(token)
                params[token_key] = token
            else:
                raise ContractError("Amazon max_pages exceeded; cursor not advanced")
        # Advance to the completed window boundary only after ALL pages, never by page maximum.
        yield Checkpoint(stream, {"updated_since": until.isoformat()})
