"""TikTok Shop Orders 202309: signed requests, bounded time windows, PII-free order lines.

Shop inventory is supplied through WMS/CSV until a market-specific inventory contract is verified.
Never reinterpret listed product quantity as physical warehouse stock.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..markets import TIKTOK_MARKETS
from ...core.util import utcnow
from ..errors import AuthError, ContractError
from ..spi import Checkpoint, HealthResult, RawItem
from ..streams import parse_datetime
from .platform_http import send_json

BASE_URL = "https://open-api.tiktokglobalshop.com"


def sign_request(secret: str, path: str, params: dict, body: bytes = b"") -> str:
    query = "".join(f"{key}{params[key]}" for key in sorted(params) if key not in ("sign", "access_token"))
    message = secret.encode() + path.encode() + query.encode() + body + secret.encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class TikTokConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_cipher: str = Field(min_length=1)
    shop_id: str = Field(min_length=1, max_length=40)
    market: str
    location_code: str = Field(min_length=1, max_length=64)
    initial_since: str
    lookback_minutes: int = Field(120, ge=120, le=10080)
    max_pages: int = Field(500, ge=1, le=10000)
    rate_limit: dict = Field(default_factory=lambda: {"rate_per_second": 1, "burst": 1})

    @field_validator("market")
    @classmethod
    def market_known(cls, value):
        value = value.upper().replace("UK", "GB")
        if value not in TIKTOK_MARKETS:
            raise ValueError("market must be a configured TikTok Shop market")
        return value


class TikTokShopConnector:
    kind = "tiktok_shop"
    version = "202309"

    def validate_config(self, config, settings=None):
        result = TikTokConfig.model_validate(config)
        parse_datetime(result.initial_since, "UTC")
        return result.model_dump(mode="json")

    def streams(self, config):
        return ["order_lines"]

    def count(self, ctx, stream, window):
        return None

    def _client(self):
        return httpx.Client(timeout=30, follow_redirects=False, trust_env=False)

    def _credentials(self, client, ctx):
        now = (ctx.now or utcnow)().timestamp()
        if not all(ctx.secrets.get(k) for k in ("app_key", "app_secret")):
            raise AuthError("TikTok app_key and app_secret required")
        if ctx.secrets.get("access_token") and float(ctx.secrets.get("access_token_expire_in", 0)) > now + 60:
            return ctx.secrets
        if ctx.secrets.get("refresh_token") and ctx.save_secrets is not None:
            document = send_json(client, ctx, "GET", "https://auth.tiktok-shops.com/api/v2/token/refresh", endpoint="tiktok:refresh",
                params={"app_key": ctx.secrets["app_key"], "app_secret": ctx.secrets["app_secret"],
                        "refresh_token": ctx.secrets["refresh_token"], "grant_type": "refresh_token"})
            if document.get("code") != 0 or not (document.get("data") or {}).get("access_token"):
                raise AuthError("TikTok token refresh rejected")
            data = document["data"]
            updated = {**ctx.secrets, **{k: data[k] for k in ("access_token", "refresh_token", "access_token_expire_in", "refresh_token_expire_in") if k in data}}
            ctx.save_secrets(updated)  # Commit a rotated refresh token BEFORE pulling orders.
            ctx.secrets = updated
        elif not ctx.secrets.get("access_token"):
            raise AuthError("TikTok access_token or durable refresh-token context required")
        return ctx.secrets

    def _request(self, client, ctx, config, method, path, params, body=None):
        credentials = self._credentials(client, ctx)
        now = (ctx.now or utcnow)()
        params = {**params, "app_key": credentials["app_key"], "timestamp": int(now.timestamp())}
        if path.startswith("/order/"):
            params["shop_cipher"] = config.shop_cipher
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body is not None else b""
        params["sign"] = sign_request(credentials["app_secret"], path, params, raw)
        result = send_json(client, ctx, method, BASE_URL + path, endpoint=f"tiktok:{path}", params=params, content=raw,
                           headers={"x-tts-access-token": credentials["access_token"], "Content-Type": "application/json"})
        if result.get("code") != 0:
            # Unknown business errors halt the stream; never advance a cursor on a 200 error envelope.
            raise ContractError(f"TikTok business error code {result.get('code')}; authorization/scopes may need repair")
        if not isinstance(result.get("data"), dict):
            raise ContractError("TikTok response missing data")
        return result["data"]

    def check(self, ctx):
        config = TikTokConfig.model_validate(ctx.config)
        with self._client() as client:
            data = self._request(client, ctx, config, "GET", "/authorization/202309/shops", {})
        matched = any(s.get("cipher") == config.shop_cipher and str(s.get("id")) == config.shop_id
                      and str(s.get("region", "")).upper().replace("UK", "GB") == config.market
                      for s in data.get("shops", []))
        return HealthResult(matched, "shop id, cipher and market authorization checked")

    def read(self, ctx, stream, cursor, window):
        if stream != "order_lines":
            raise ContractError("unsupported TikTok stream")
        config = TikTokConfig.model_validate(ctx.config)
        since = window.start if window else parse_datetime((cursor or {}).get("updated_since") or config.initial_since, "UTC") - timedelta(minutes=config.lookback_minutes)
        until = window.end if window else (ctx.now or utcnow)()
        if since >= until:
            return
        with self._client() as client:
            # Small windows limit payload size and allow resumable checkpoints on large backfills.
            while since < until:
                end = min(until, since + timedelta(days=1))
                body = {"update_time_ge": int(since.timestamp()), "update_time_lt": int(end.timestamp())}
                params = {"page_size": 100, "sort_field": "update_time", "sort_order": "ASC"}
                seen = set()
                for _ in range(config.max_pages):
                    data = self._request(client, ctx, config, "POST", "/order/202309/orders/search", params, body)
                    orders = data.get("orders")
                    if not isinstance(orders, list):
                        raise ContractError("TikTok response missing orders")
                    for order in orders:
                        updated = datetime.fromtimestamp(order["update_time"], UTC)
                        created = datetime.fromtimestamp(order["create_time"], UTC).isoformat()
                        for item in order["line_items"]:
                            # One entry represents one unit in this API version. Combined listing components
                            # identify the original seller SKUs; preserve stable child keys across replays.
                            children = item.get("combined_listing_skus") or [{"seller_sku": item.get("seller_sku"), "sku_count": 1, "sku_id": item.get("sku_id")}]
                            for child in children:
                                line_id = str(item["id"]) + (":" + str(child["sku_id"]) if item.get("combined_listing_skus") else "")
                                payload = {"order_id": str(order["id"]), "line_id": line_id,
                                           "sku": child.get("seller_sku"), "location": config.location_code,
                                           "quantity": child["sku_count"], "status": item.get("display_status", order["status"]).lower(),
                                           "ordered_at": created, "channel": f"tiktok:{config.shop_id}:{config.market}"}
                                yield RawItem(stream, f'{order["id"]}|{line_id}', payload, updated)
                    token = data.get("next_page_token")
                    if not token:
                        break
                    if token in seen:
                        raise ContractError("TikTok pagination token repeated")
                    seen.add(token); params["page_token"] = token
                else:
                    raise ContractError("TikTok max_pages exceeded; cursor not advanced")
                # Advance to fixed window end, not max(record.update_time); responses can exceed the filter.
                yield Checkpoint(stream, {"updated_since": end.isoformat()})
                since = end
