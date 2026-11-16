"""Contract fixtures run through the actual platform parsers. Transport cannot reach a network."""
from __future__ import annotations
import copy
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
import httpx
from ...core.audit import record_audit
from ...core.crypto import encrypt_json
from ...core.errors import InvalidInput
from ...core.util import utcnow
from ..errors import AuthError, ContractError
from ..spi import RawItem, SyncWindow
from ..streams import parse_datetime
from .amazon_sp import AmazonSpConnector
from .tiktok_shop import TikTokShopConnector

FIXTURE_VERSION = 'official-projection-v1'
FIXTURE_TIME = datetime(2026, 10, 1, 12, tzinfo=UTC)


def official_fixture(platform: str, config: dict):
    if platform == 'amazon_sp':
        return {'orders': [{'orderId': f'MOCK-A-{i}', 'createdTime': FIXTURE_TIME.isoformat(),
            'lastUpdatedTime': FIXTURE_TIME.isoformat(), 'fulfillment': {'fulfillmentStatus': status},
            'orderItems': [{'orderItemId': f'L-{i}', 'product': {'sellerSku': 'S'}, 'quantityOrdered': qty}]}
            for i, (status, qty) in enumerate((('UNSHIPPED', 5), ('PENDING', 2), ('CANCELLED', 1)), 1)]}
    return {'code': 0, 'data': {'orders': [{'id': f'MOCK-T-{i}', 'create_time': int(FIXTURE_TIME.timestamp()),
        'update_time': int(FIXTURE_TIME.timestamp()), 'status': status,
        'line_items': [{'id': f'L-{i}', 'seller_sku': 'S', 'sku_id': 'MOCK-SKU', 'display_status': status}]}
        for i, status in enumerate(('AWAITING_SHIPMENT', 'UNPAID', 'CANCELLED'), 1)]}}


def _transport(ctx, platform):
    def handler(request):
        config, path, q = ctx.config, request.url.path, request.url.params
        if not ctx.secrets.get('mock_authorized'):
            return httpx.Response(401, json={'error': 'mock authorization required'})
        if path == '/auth/o2/token':
            return httpx.Response(200, json={'access_token': 'MOCK-ACCESS-NO-LIVE-USE', 'expires_in': 3600})
        if path == '/sellers/v1/marketplaceParticipations':
            return httpx.Response(200, json={'payload': [{'marketplace': {'id': config['marketplace_id']}, 'participation': {'isParticipating': True}}]})
        if path == '/authorization/202309/shops':
            return httpx.Response(200, json={'code': 0, 'data': {'shops': [{'id': config['shop_id'], 'cipher': config['shop_cipher'], 'region': config['market']}]}})
        if path == '/orders/2026-01-01/orders':
            fixture = official_fixture(platform, config)
            start, end = parse_datetime(q['lastUpdatedAfter'], 'UTC'), parse_datetime(q['lastUpdatedBefore'], 'UTC')
            orders = fixture['orders'] if start <= FIXTURE_TIME < end else []
            # Deliberately two pages so the normal cursor / completeness code is exercised.
            return httpx.Response(200, json={'orders': orders[2:] if q.get('paginationToken') else orders[:2],
                **({'pagination': {'nextToken': 'MOCK-PAGE-2'}} if len(orders)>2 and not q.get('paginationToken') else {})})
        if path == '/fba/inventory/v1/summaries':
            return httpx.Response(200, json={'payload': {'inventorySummaries': [{'sellerSku': 'S', 'inventoryDetails': {
                'fulfillableQuantity': 120, 'reservedQuantity': {'totalReservedQuantity': 7},
                'inboundWorkingQuantity': 2, 'inboundShippedQuantity': 3, 'inboundReceivingQuantity': 1}}]}})
        if path == '/order/202309/orders/search':
            body = json.loads(request.content)
            fixture = official_fixture(platform, config)
            if not body['update_time_ge'] <= int(FIXTURE_TIME.timestamp()) < body['update_time_lt']:
                fixture['data']['orders'] = []
            return httpx.Response(200, json=fixture)
        # Unknown endpoints fail loudly; no fallback to a real HTTP transport.
        raise ContractError('unrecognized mock platform endpoint')
    return httpx.MockTransport(handler)


class _MockPlatform:
    def _real(self, ctx):
        connector = self.base()
        connector._client = lambda: httpx.Client(transport=_transport(ctx, self.base.kind), trust_env=False)
        ctx.rate_limiter.configure('amazon:/orders/2026-01-01/orders', 10000, 10000)
        ctx.rate_limiter.configure('tiktok:/order/202309/orders/search', 10000, 10000)
        return connector

    def validate_config(self, config, settings=None):
        return self.base().validate_config(config, settings)

    def streams(self, config):
        return self.base().streams(config)

    def count(self, ctx, stream, window):
        return None

    def check(self, ctx):
        return self._real(ctx).check(ctx)

    def read(self, ctx, stream, cursor, window):
        yield from self._real(ctx).read(ctx, stream, cursor, window)


class MockAmazonConnector(_MockPlatform):
    kind = 'mock_amazon'
    version = FIXTURE_VERSION
    base = AmazonSpConnector


class MockTikTokConnector(_MockPlatform):
    kind = 'mock_tiktok'
    version = FIXTURE_VERSION
    base = TikTokShopConnector


def authorize(session, principal, connection):
    if connection.kind not in ('mock_amazon', 'mock_tiktok') or not connection.is_mock:
        raise InvalidInput('mock authorization is restricted to explicit mock connectors')
    connection.secret_ciphertext = encrypt_json({'mock_authorized': True, 'client_id': 'MOCK', 'client_secret': 'MOCK',
        'refresh_token': 'MOCK-REFRESH-NO-LIVE-USE', 'app_key': 'MOCK', 'app_secret': 'MOCK',
        'access_token': 'MOCK-ACCESS-NO-LIVE-USE', 'access_token_expire_in': 4102444800})
    connection.status = 'active'
    record_audit(session, principal, 'integration.mock.authorize', 'integration_connection', connection.id,
                 after={'fixture_version': FIXTURE_VERSION, 'is_mock': True})


def manifest_for_batch(session, connection, batch):
    from ..connections import build_ctx
    if not connection.is_mock or connection.kind not in ('mock_amazon', 'mock_tiktok'):
        raise InvalidInput('independent mock manifest only available for mock platforms')
    ctx = build_ctx(connection)
    clock = parse_datetime(batch.scope['snapshot_at'], 'UTC')
    ctx.now = lambda: clock
    connector = MockAmazonConnector() if connection.kind == 'mock_amazon' else MockTikTokConnector()
    window = SyncWindow(batch.window_start, batch.window_end) if batch.stream == 'order_lines' else None
    rows = [{'external_id': r.external_id, 'payload': r.payload, 'source_updated_at': r.source_updated_at}
            for r in connector.read(ctx, batch.stream, None, window) if isinstance(r, RawItem)]
    return {'source_rows': rows, 'scope': batch.scope, 'evidence': 'independent official-shaped fixture '+FIXTURE_VERSION, 'execution_refs': {}}
