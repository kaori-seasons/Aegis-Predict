from datetime import UTC, datetime
import uuid

import httpx
import pytest

from aegis.core.config import Settings
from aegis.integration.connectors.amazon_sp import AmazonSpConnector
from aegis.integration.connectors.tiktok_shop import TikTokShopConnector, sign_request
from aegis.integration.connectors.generic_rest import GenericRestConnector
from aegis.integration.connectors.file_csv import FileCsvConnector
from aegis.integration.errors import ContractError, TransientError
from aegis.integration.ratelimit import RateLimiter
from aegis.integration.spi import Checkpoint, ConnCtx, RawItem, WritebackRequest

NOW = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)


def ctx(config, secrets=None):
    return ConnCtx(uuid.uuid4(), uuid.uuid4(), "store-1", config, secrets or {}, Settings(env="test"),
                   RateLimiter(100000, 100000), sleep=lambda _: None, now=lambda: NOW)


def test_tiktok_signature_known_vector_and_body_bytes():
    assert sign_request("e59af819cc", "/authorization/202309/shops", {"app_key": "29a39d", "timestamp": 1623812664}) == "b596b73e0cc6de07ac26f036364178ab16b0a907af13d43f0a0cd2345f582dc8"
    assert sign_request("s", "/p", {"b": 2, "a": 1}, b'{}') != sign_request("s", "/p", {"a": 1, "b": 2}, b'{ }')


def test_amazon_2026_pagination_projection_and_complete_checkpoint(monkeypatch):
    connector = AmazonSpConnector()
    config = connector.validate_config({"region": "NA", "marketplace_id": "ATVPDKIKX0DER", "seller_id": "seller-1", "location_code": "W", "initial_since": "2026-11-14T00:00:00Z"})
    context = ctx(config, {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"})
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["x-amz-access-token"] == "token"
        assert request.url.path == "/orders/2026-01-01/orders"
        assert request.url.params["includedData"] == "FULFILLMENT"
        item = {"orderId": "O", "createdTime": "2026-11-14T00:00:00Z", "lastUpdatedTime": "2026-11-15T00:00:00Z",
                "fulfillment": {"fulfillmentStatus": "SHIPPED"}, "buyer": {"buyerEmail": "private@example.com"},
                "orderItems": [{"orderItemId": "L", "quantityOrdered": 2, "product": {"sellerSku": "S"}}]}
        if "paginationToken" in request.url.params:
            return httpx.Response(200, json={"orders": []})
        return httpx.Response(200, json={"orders": [item], "pagination": {"nextToken": "next"}})
    monkeypatch.setattr(connector, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    records = list(connector.read(context, "order_lines", None, None))
    assert sum(isinstance(r, RawItem) for r in records) == 1
    assert "private@example.com" not in repr(records)
    assert len([r for r in records if isinstance(r, Checkpoint)]) == 1
    assert records[-1].cursor["updated_since"] == "2026-11-15T23:58:00+00:00"
    assert len(calls) == 3


def test_amazon_inventory_is_full_snapshot_and_no_double_reserved(monkeypatch):
    c = AmazonSpConnector()
    config = c.validate_config({"region": "EU", "marketplace_id": "A1PA6795UKMFR9", "seller_id": "seller-1", "location_code": "W", "initial_since": "2026-11-14T00:00:00Z"})
    context = ctx(config, {"_access_token": "token", "_expires_at": NOW.timestamp()+1000})
    def handler(request):
        assert "startDateTime" not in request.url.params
        return httpx.Response(200, json={"payload": {"inventorySummaries": [{"sellerSku": "S", "inventoryDetails": {
            "fulfillableQuantity": 10, "reservedQuantity": {"totalReservedQuantity": 5}, "inboundWorkingQuantity": 2,
            "inboundShippedQuantity": 3, "inboundReceivingQuantity": 4}}]}})
    monkeypatch.setattr(c, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    record = list(c.read(context, "inventory_snapshots", None, None))[0]
    assert record.payload["on_hand"] == 10
    assert record.payload["allocated"] == 0
    assert record.payload["in_transit"] == 9


def test_tiktok_orders_child_skus_and_pii_projection(monkeypatch):
    c = TikTokShopConnector()
    context = ctx(c.validate_config({"shop_id": "shop-1", "market": "US", "shop_cipher": "cipher", "location_code": "W", "initial_since": "2026-11-15T12:00:00Z"}),
                  {"app_key": "a", "app_secret": "s", "access_token": "t", "access_token_expire_in": NOW.timestamp()+3600})
    def handler(request):
        assert request.headers["x-tts-access-token"] == "t"
        assert request.url.params["shop_cipher"] == "cipher"
        params = dict(request.url.params)
        assert params["sign"] == sign_request("s", request.url.path, params, request.content)
        order = {"id": "O", "create_time": 1794690000, "update_time": 1794695000, "status": "COMPLETED",
                 "recipient_address": {"name": "PRIVATE"}, "line_items": [{"id": "L", "sku_id": "bundle", "seller_sku": "B",
                 "combined_listing_skus": [{"sku_id": "child", "seller_sku": "S", "sku_count": 2}]}]}
        return httpx.Response(200, json={"code": 0, "data": {"orders": [order]}})
    monkeypatch.setattr(c, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    records = list(c.read(context, "order_lines", None, None))
    assert records[0].payload["sku"] == "S"
    assert records[0].payload["quantity"] == 2
    assert "PRIVATE" not in repr(records)


def test_tiktok_http_200_business_error_does_not_checkpoint(monkeypatch):
    c = TikTokShopConnector()
    context = ctx(c.validate_config({"shop_id": "shop-1", "market": "US", "shop_cipher": "x", "location_code": "W", "initial_since": "2026-11-15T00:00:00Z"}),
                  {"app_key": "a", "app_secret": "s", "access_token": "t", "access_token_expire_in": NOW.timestamp()+3600})
    monkeypatch.setattr(c, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 123}))))
    with pytest.raises(ContractError, match="business error"):
        list(c.read(context, "order_lines", None, None))


def test_csv_cannot_read_another_tenant_inbox(tmp_path):
    c = FileCsvConnector()
    context = ctx({"directory": "another-tenant"})
    context.settings.file_inbox_root = tmp_path
    with pytest.raises(ContractError, match="tenant"):
        c._directory(context)


def test_generic_rest_rejects_cross_origin_path_before_auth():
    c = GenericRestConnector()
    settings = Settings(env="test", connector_allow_insecure_http=True, connector_allow_private_network=True)
    config = c._config(ctx({"base_url": "https://example.com", "streams": {"order_lines": {"path": "/orders"}}}))
    context = ctx({}); context.settings = settings
    with httpx.Client() as client:
        with pytest.raises(ContractError, match="origin"):
            c._send(context, client, config, "GET", "https://attacker.example/orders")


def test_market_profiles_reject_wrong_region_and_unknown_market():
    from aegis.integration.markets import catalogue
    assert len(catalogue()["amazon"]) == 23
    assert len(catalogue()["tiktok_shop"]) == 20
    assert "configuration-only" in catalogue()["verification_level"]
    with pytest.raises(ValueError, match="region"):
        AmazonSpConnector().validate_config({"region": "NA", "marketplace_id": "A1PA6795UKMFR9",
            "seller_id": "seller", "location_code": "W", "initial_since": "2026-01-01T00:00:00Z"})
    with pytest.raises(ValueError, match="configured TikTok"):
        TikTokShopConnector().validate_config({"shop_id": "s", "shop_cipher": "c", "market": "XX",
            "location_code": "W", "initial_since": "2026-01-01T00:00:00Z"})
