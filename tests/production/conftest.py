from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from aegis.core.config import reset_settings
from aegis.core.crypto import generate_key
from aegis.core.db import dispose_engines


@pytest.fixture(scope="session")
def pg():
    dsn = os.environ.get("AEGIS_TEST_ADMIN_DSN")
    if not dsn:
        pytest.skip("set AEGIS_TEST_ADMIN_DSN to a disposable PostgreSQL test database")
    url = make_url(dsn)
    if not (url.database or "").endswith("_test"):
        pytest.fail("test database name must end in _test")
    os.environ["AEGIS_ADMIN_DATABASE_URL"] = dsn
    os.environ["AEGIS_DATABASE_URL"] = url.set(username="aegis_app", password=os.environ.get("AEGIS_TEST_APP_PASSWORD")).render_as_string(hide_password=False)
    os.environ["AEGIS_ENV"] = "test"
    os.environ["AEGIS_AUTH_MODE"] = "password"
    os.environ["AEGIS_REQUIRE_RECONCILIATION"] = "false"
    os.environ["AEGIS_INTEGRATION_MODE"] = "live"
    os.environ["AEGIS_COOKIE_SECURE"] = "false"
    os.environ["AEGIS_PLAN_EXECUTION_ENABLED"] = "true"  # Synthetic fault-injection tests only.
    os.environ["AEGIS_SECRET_KEYS"] = generate_key()
    reset_settings(); dispose_engines()
    from aegis.core.dbadmin import ensure_app_role, grant_app_role, upgrade
    engine = create_engine(dsn)
    with engine.begin() as c:
        ensure_app_role(c, "aegis_app", os.environ.get("AEGIS_TEST_APP_PASSWORD"))
    upgrade(url=dsn)
    with engine.begin() as c:
        grant_app_role(c, "aegis_app")
    yield
    dispose_engines(); engine.dispose()


@pytest.fixture
def tenant(pg):
    from aegis.bootstrap import bootstrap_tenant
    return bootstrap_tenant(f"t-{uuid.uuid4().hex[:12]}", "Test tenant", "admin@test.local", "test-password-12345")


@pytest.fixture
def seeded(tenant):
    from aegis.core.db import tenant_session
    from aegis.core.tenancy import system_principal
    from aegis.core.util import utcnow
    from aegis.integration.connections import create_connection
    from aegis.integration.raw import land_batch
    from aegis.integration.normalize import normalize_batch
    from aegis.integration.spi import RawItem
    p = system_principal(tenant)
    now = utcnow()
    values = [
        ("skus", "S", {"sku": "S", "name": "Test SKU", "pack_size": 5, "unit_margin": 10, "service_class": "standard", "category": "c"}),
        ("locations", "GLOBAL-POOL", {"code": "GLOBAL-POOL", "kind": "pool", "timezone": "UTC"}),
        ("locations", "W", {"code": "W", "kind": "warehouse", "timezone": "America/Los_Angeles", "handling_capacity_per_day": 1000,
                             "capacity_units": 10000, "max_daily_inbound_units": 1000}),
        ("lanes", "pool-W", {"from": "GLOBAL-POOL", "to": "W", "lead_time_days": 10, "lead_time_std_days": 2, "unit_cost": 1}),
        ("transport_options", "sea", {"from": "GLOBAL-POOL", "to": "W", "mode": "SEA", "lead_time_days": 10, "unit_cost": 1, "reliability": 1, "max_units": 1000}),
        ("inventory_snapshots", "S-pool", {"sku": "S", "location": "GLOBAL-POOL", "as_of": now.isoformat(), "on_hand": 1000, "allocated": 20}),
        ("inventory_snapshots", "S-W", {"sku": "S", "location": "W", "as_of": now.isoformat(), "on_hand": 1, "in_transit": 2, "allocated": 3}),
    ]
    for i in range(60):
        day = (now.date() - timedelta(days=61-i)).isoformat()
        values.append(("daily_demand", day, {"sku": "S", "location": "W", "date": day, "units": 10, "stockout": False}))
    items = [RawItem(s, key, body, now) for s, key, body in values]
    with tenant_session(tenant) as session:
        conn = create_connection(session, p, name="test", kind="file_csv", config={"directory": str(tenant)})
        connection_id = conn.id
        from aegis.integration.batches import start_batch, finish_landing
        for stream in sorted({item.stream for item in items}):
            batch = start_batch(session, tenant, connection_id, stream)
            land_batch(session, tenant_id=tenant, connection_id=connection_id, sync_run_id=None,
                       items=[item for item in items if item.stream == stream], batch_id=batch.id)
            finish_landing(session, batch.id)
        stats = normalize_batch(session, tenant, limit=1000)
        assert stats.quarantined == 0
    from aegis.integration.authority import set_authority
    from aegis.models import Location
    with tenant_session(tenant) as session:
        for loc in session.scalars(select(Location)):
            set_authority(session, p, domain="inventory", location_id=loc.id, connection_id=connection_id,
                          stream="inventory_snapshots", note="fixture inventory contract")
            if loc.kind != "pool":
                set_authority(session, p, domain="demand", location_id=loc.id, connection_id=connection_id,
                              stream="daily_demand", note="fixture daily totals contract")
    return tenant, connection_id, items


@pytest.fixture
def other_tenant(pg):
    from aegis.bootstrap import bootstrap_tenant
    return bootstrap_tenant(f"other-{uuid.uuid4().hex[:12]}", "Other tenant", "admin@test.local", "test-password-12345")
