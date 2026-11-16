"""Google authorization code + PKCE/OIDC. No email-based account linking."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select
from ..core.config import get_settings
from ..core.crypto import decrypt_json, encrypt_json
from ..core.db import global_session, tenant_session
from ..core.errors import ConfigurationError, Unauthenticated, InvalidInput
from ..core.security import issue_session
from ..core.util import utcnow
from ..core.audit import record_audit
from ..core.tenancy import system_principal
from ..models import OAuthAttempt, ExternalIdentity, AppUser, Tenant

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
CALLBACK_PATH = "/api/v1/auth/google/callback"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _client():
    return httpx.Client(timeout=15, follow_redirects=False, trust_env=False)


def configuration():
    s = get_settings()
    uri = urlsplit(s.google_redirect_uri)
    if not s.google_client_id or not s.google_client_secret.get_secret_value():
        raise ConfigurationError("Google client credentials must be configured")
    if uri.path != CALLBACK_PATH or uri.query or uri.fragment or uri.username or uri.password:
        raise ConfigurationError("Google redirect URI must use the fixed callback path")
    if uri.scheme != "https" and not (s.env != "prod" and uri.scheme == "http" and uri.hostname in ("localhost", "127.0.0.1", "testserver")):
        raise ConfigurationError("Google callback must use HTTPS")
    return s


def begin(tenant_slug: str):
    s = configuration()
    with global_session() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == tenant_slug, Tenant.status == "active"))
    if tenant is None:
        raise Unauthenticated("workspace unavailable")
    attempt_id = uuid.uuid4()
    state = f"{tenant.id}.{attempt_id}.{secrets.token_urlsafe(32)}"
    browser = secrets.token_urlsafe(32)
    verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with tenant_session(tenant.id) as session:
        session.add(OAuthAttempt(id=attempt_id, tenant_id=tenant.id, state_hash=digest(state),
            browser_hash=digest(browser), secret_ciphertext=encrypt_json({"verifier": verifier, "nonce": nonce}),
            expires_at=utcnow()+timedelta(minutes=10), status="pending"))
    query = {"response_type": "code", "client_id": s.google_client_id, "redirect_uri": s.google_redirect_uri,
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
        "code_challenge_method": "S256", "prompt": "select_account"}
    return {"authorization_url": AUTH_URL+'?'+urlencode(query), "state": state,
            "browser": browser, "cookie_name": "aegis_oidc_"+attempt_id.hex}


def state_ids(state: str):
    try:
        if len(state) > 256:
            raise ValueError()
        tenant, attempt, random = state.split('.')
        if len(random) < 40:
            raise ValueError()
        return uuid.UUID(tenant), uuid.UUID(attempt)
    except (ValueError, TypeError, AttributeError) as exc:
        raise Unauthenticated("invalid OAuth state") from exc


def verify_id_token(token: str, jwks: dict, *, nonce: str, now: float | None = None) -> dict:
    s = get_settings()
    now = now or utcnow().timestamp()
    try:
        if len(token) > 32768:
            raise ValueError()
        header_part, payload_part, signature = token.split('.')
        header, claims = json.loads(_b64(header_part)), json.loads(_b64(payload_part))
        if header.get('alg') != 'RS256' or header.get('crit') or not isinstance(header.get('kid'), str):
            raise ValueError()
        matches = [key for key in jwks['keys'] if key.get('kid') == header['kid'] and key.get('kty') == 'RSA' and key.get('use', 'sig') == 'sig' and key.get('alg', 'RS256') == 'RS256']
        if len(matches) != 1:
            raise ValueError()
        key = matches[0]
        public = rsa.RSAPublicNumbers(int.from_bytes(_b64(key['e']), 'big'), int.from_bytes(_b64(key['n']), 'big')).public_key()
        if not 2048 <= public.key_size <= 8192:
            raise ValueError()
        public.verify(_b64(signature), (header_part+'.'+payload_part).encode(), padding.PKCS1v15(), hashes.SHA256())
        aud = claims.get('aud')
        if aud != s.google_client_id and not (isinstance(aud, list) and s.google_client_id in aud):
            raise ValueError()
        if (isinstance(aud, list) and len(aud) > 1 or 'azp' in claims) and claims.get('azp') != s.google_client_id:
            raise ValueError()
        if claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com'):
            raise ValueError()
        for field in ('exp', 'iat'):
            if type(claims.get(field)) is not int:
                raise ValueError()
        if claims['exp'] <= now or claims['iat'] > now+60 or claims.get('nbf', 0) > now+60:
            raise ValueError()
        if not isinstance(claims.get('nonce'), str) or not hmac.compare_digest(claims['nonce'], nonce):
            raise ValueError()
        if claims.get('email_verified') is not True or not isinstance(claims.get('email'), str):
            raise ValueError()
        if not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255:
            raise ValueError()
        if s.google_allowed_domains and claims.get('hd', '').lower() not in [v.lower() for v in s.google_allowed_domains]:
            raise ValueError()
        return claims
    except Exception as exc:
        raise Unauthenticated("Google identity token validation failed") from exc


def complete(state: str, code: str, browser: str, *, ip=None, user_agent=None):
    s = configuration()
    tenant_id, attempt_id = state_ids(state)
    # Consume once BEFORE the network request. A timed-out code exchange must be restarted.
    with tenant_session(tenant_id) as session:
        row = session.get(OAuthAttempt, attempt_id, with_for_update=True)
        if row is None or row.status != 'pending' or row.expires_at <= utcnow() or not hmac.compare_digest(row.state_hash, digest(state)) or not hmac.compare_digest(row.browser_hash, digest(browser or '')):
            raise Unauthenticated("OAuth state expired, replayed or browser binding invalid")
        row.status = 'exchanging'
        secret = decrypt_json(row.secret_ciphertext)
    try:
        if not code or len(code) > 4096:
            raise Unauthenticated("authorization denied")
        with _client() as client:
            response = client.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                "client_id": s.google_client_id, "client_secret": s.google_client_secret.get_secret_value(),
                "redirect_uri": s.google_redirect_uri, "code_verifier": secret['verifier']})
            if response.status_code != 200:
                raise Unauthenticated("Google code exchange rejected")
            token = response.json().get('id_token', '')
            keys = client.get(JWKS_URL)
            if keys.status_code != 200:
                raise Unauthenticated("Google signing keys unavailable")
            claims = verify_id_token(token, keys.json(), nonce=secret['nonce'])
        with tenant_session(tenant_id) as session:
            tenant = session.get(Tenant, tenant_id)
            binding = session.scalar(select(ExternalIdentity).where(ExternalIdentity.provider == 'google', ExternalIdentity.subject == claims['sub']))
            user = session.get(AppUser, binding.user_id, with_for_update=True) if binding else None
            if tenant is None or tenant.status != 'active' or user is None or not user.is_active:
                raise Unauthenticated("Google identity is not bound to an active workspace member")
            issued = issue_session(session, user, ip=ip, user_agent=user_agent)
            row = session.get(OAuthAttempt, attempt_id, with_for_update=True)
            row.status, row.secret_ciphertext = 'succeeded', encrypt_json({})
            record_audit(session, system_principal(tenant_id, user.email), 'auth.google.login', 'app_user', user.id)
            return issued
    except Exception as exc:
        with tenant_session(tenant_id) as session:
            row = session.get(OAuthAttempt, attempt_id, with_for_update=True)
            row.status, row.secret_ciphertext = 'failed', encrypt_json({})
        if isinstance(exc, Unauthenticated):
            raise
        raise Unauthenticated("Google authorization failed; restart sign-in") from exc


def bind_identity(session, principal, user_id: uuid.UUID, subject: str):
    user = session.get(AppUser, user_id, with_for_update=True)
    if user is None or user.tenant_id != principal.tenant_id or not subject.strip() or len(subject) > 255:
        raise InvalidInput("active user and Google subject are required")
    row = ExternalIdentity(tenant_id=principal.tenant_id, user_id=user.id, provider='google', subject=subject)
    session.add(row); session.flush()
    record_audit(session, principal, 'identity.google.bind', 'external_identity', row.id, after={"user_id": user.id, "subject": subject})
    return row
