from __future__ import annotations
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from aegis.auth import google
from aegis.core.config import get_settings
from aegis.core.crypto import decrypt_json
from aegis.core.db import tenant_session
from aegis.core.errors import Unauthenticated
from aegis.core.tenancy import system_principal
from aegis.models import AppUser, OAuthAttempt, Tenant


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


@pytest.fixture
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    jwks = {'keys': [{'kid': 'test-key', 'kty': 'RSA', 'use': 'sig', 'alg': 'RS256',
        'n': b64(nums.n.to_bytes(256, 'big')), 'e': b64(nums.e.to_bytes(3, 'big'))}]}
    def sign(claims, alg='RS256'):
        message = b64(json.dumps({'alg': alg, 'kid': 'test-key'}).encode())+'.'+b64(json.dumps(claims).encode())
        return message+'.'+b64(key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256()))
    return sign, jwks


@pytest.fixture
def google_config(pg, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'auth_mode', 'google')
    monkeypatch.setattr(settings, 'google_client_id', 'test-client.apps.googleusercontent.com')
    monkeypatch.setattr(settings, 'google_client_secret', SecretStr('test-client-secret'))
    monkeypatch.setattr(settings, 'google_redirect_uri', 'http://testserver'+google.CALLBACK_PATH)
    return settings


def claims(settings, nonce='nonce'):
    return dict(iss='https://accounts.google.com', aud=settings.google_client_id, sub='google-subject',
                email='admin@test.local', email_verified=True, nonce=nonce, iat=int(time.time()), exp=int(time.time())+300)


@pytest.mark.parametrize('change', [dict(aud='another-client'), dict(iss='https://attacker.invalid'), dict(exp=1),
    dict(nonce='replayed'), dict(email_verified=False), dict(azp='other'), dict(sub=''), dict(iat=True)])
def test_google_jwt_rejects_invalid_claims(google_config, signer, change):
    sign, keys = signer
    with pytest.raises(Unauthenticated):
        google.verify_id_token(sign({**claims(google_config), **change}), keys, nonce='nonce')


def test_google_code_flow_cookie_binding_pkce_replay_and_no_email_link(google_config, signer, tenant, monkeypatch):
    sign, keys = signer
    with tenant_session(tenant) as s:
        slug = s.get(Tenant, tenant).slug
        user = s.scalar(select(AppUser))
        google.bind_identity(s, system_principal(tenant), user.id, 'google-subject')
    from aegis.api.app import create_app
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/auth/login', json={'tenant': slug, 'email': 'admin@test.local', 'password': 'test-password-12345'}).status_code == 403
        start = client.post('/api/v1/auth/google/start', json={'tenant': slug})
        assert start.status_code == 200
        query = parse_qs(urlsplit(start.json()['authorization_url']).query)
        assert query['code_challenge_method'] == ['S256']
        state = query['state'][0]
        with tenant_session(tenant) as s:
            row = s.get(OAuthAttempt, google.state_ids(state)[1])
            verifier = decrypt_json(row.secret_ciphertext)['verifier']
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if str(request.url) == google.TOKEN_URL:
                form = parse_qs(request.content.decode())
                assert form['code_verifier'] == [verifier]
                return httpx.Response(200, json={'id_token': sign(claims(google_config, query['nonce'][0]))})
            return httpx.Response(200, json=keys)
        monkeypatch.setattr(google, '_client', lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        # Another browser cannot consume the pending attempt.
        with TestClient(create_app()) as other:
            assert other.get(google.CALLBACK_PATH, params={'state': state, 'code': 'code'}).status_code == 401
        callback = client.get(google.CALLBACK_PATH, params={'state': state, 'code': 'code'}, follow_redirects=False)
        assert callback.status_code == 303
        assert client.get('/api/v1/auth/me').status_code == 200
        assert client.get(google.CALLBACK_PATH, params={'state': state, 'code': 'code'}).status_code == 401
        assert len(calls) == 2
        # Verified email alone is insufficient: an unbound subject cannot take over this member.
        flow = google.begin(slug)
        def unbound(request):
            return httpx.Response(200, json=({'id_token': sign({**claims(google_config, parse_qs(urlsplit(flow['authorization_url']).query)['nonce'][0]), 'sub': 'unbound-subject'})} if str(request.url)==google.TOKEN_URL else keys))
        monkeypatch.setattr(google, '_client', lambda: httpx.Client(transport=httpx.MockTransport(unbound)))
        with pytest.raises(Unauthenticated, match='not bound'):
            google.complete(flow['state'], 'code', flow['browser'])
