from fastapi.testclient import TestClient
from sqlalchemy import select
from aegis.core.db import tenant_session
from aegis.models import Tenant


def login(client, tenant):
    with tenant_session(tenant) as s:
        slug = s.get(Tenant, tenant).slug
    result = client.post("/api/v1/auth/login", json={"tenant": slug, "email": "admin@test.local", "password": "test-password-12345"})
    assert result.status_code == 200, result.text
    client.headers["x-csrf-token"] = result.json()["csrf_token"]


def test_api_auth_csrf_roles_and_tenant_boundary(seeded, other_tenant):
    from aegis.api.app import create_app
    tenant, _, _ = seeded
    with TestClient(create_app()) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/plans").status_code == 401
        login(client, tenant)
        assert client.get("/api/v1/auth/me").json()["roles"] == ["TENANT_ADMIN"]
        assert set(client.get("/api/v1/integration/kinds").json()["kinds"]) >= {"amazon_sp", "tiktok_shop"}
        csrf = client.headers.pop("x-csrf-token")
        assert client.post("/api/v1/plans", json={}).status_code == 403
        client.headers["x-csrf-token"] = csrf
        result = client.post("/api/v1/users", json={"email": "viewer@test.local", "password": "viewer-password-123", "roles": ["VIEWER"]})
        assert result.status_code == 201, result.text
        assert "password_hash" not in result.json()
        created = client.post("/api/v1/plans", json={})
        assert created.status_code == 202, created.text
        plan_id = created.json()["id"]
        # Other tenant cannot resolve a known plan id.
        login(client, other_tenant)
        assert client.get(f"/api/v1/plans/{plan_id}").status_code == 404
        assert client.post("/api/v1/auth/logout").status_code == 200
        with tenant_session(tenant) as s:
            slug = s.get(Tenant, tenant).slug
        response = client.post("/api/v1/auth/login", json={"tenant": slug, "email": "viewer@test.local", "password": "viewer-password-123"})
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        assert client.post("/api/v1/plans", json={}).status_code == 403
        assert client.get("/api/v1/audit").status_code == 403
    # Cancel queued test work so subsequent worker tests claim their own run.
    from aegis.planning.lifecycle import terminate_plan
    from aegis.core.tenancy import system_principal
    import uuid
    with tenant_session(tenant) as s:
        terminate_plan(s, system_principal(tenant), uuid.UUID(plan_id), action="cancel", note="test cleanup")


def test_invalid_policy_no_internal_trace_or_secret_echo(seeded):
    from aegis.api.app import create_app
    tenant, _, _ = seeded
    with TestClient(create_app()) as client:
        login(client, tenant)
        r = client.post("/api/v1/policies", json={"solver_time_limit_seconds": -1})
        assert r.status_code == 422
        assert "traceback" not in r.text.lower()
        r = client.post("/api/v1/connections", json={"name": "tiktok", "kind": "tiktok_shop", "config": {
            "shop_id": "shop-1", "market": "US", "shop_cipher": "cipher", "location_code": "W", "initial_since": "2026-10-01T00:00:00Z"},
            "secrets": {"app_key": "key", "app_secret": "do-not-expose", "access_token": "token"}})
        assert r.status_code == 201, r.text
        assert "do-not-expose" not in r.text
        assert "secret_ciphertext" not in r.json()


def test_foundation_endpoints_and_same_shop_cannot_be_aliased(seeded, other_tenant):
    from aegis.api.app import create_app
    tenant, _, _ = seeded
    with TestClient(create_app()) as client:
        login(client, tenant)
        assert client.get("/student/health").status_code == 404
        report = client.get("/api/v1/data/pilot-readiness").json()
        assert report["data_ready"] and not report["live_pilot_approved"]
        assert client.get("/api/v1/data/authorities").json()
        assert client.get("/api/v1/data/batches").json()
        body = {"name": "US-shop", "kind": "amazon_sp", "config": {"seller_id": "seller-1",
            "marketplace_id": "ATVPDKIKX0DER", "region": "NA", "location_code": "W", "initial_since": "2026-01-01T00:00:00Z"}}
        assert client.post("/api/v1/connections", json=body).status_code == 201
        assert client.post("/api/v1/connections", json={**body, "name": "alias-US-shop"}).status_code == 409
        login(client, other_tenant)
        assert client.get("/api/v1/data/authorities").json() == []
        assert client.get("/api/v1/data/batches").json() == []
        assert client.get("/api/v1/inventory/snapshots").json() == []
        assert client.post("/api/v1/connections", json=body).status_code == 201
