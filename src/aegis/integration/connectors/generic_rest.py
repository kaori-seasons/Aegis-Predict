"""``generic_rest``: configurable JSON/REST ERP template.

Covers the common shape of Chinese cross-border ERP / WMS OpenAPIs (领星, 马帮, 谷仓, 万邑通 ...):
signed requests, page or cursor pagination, ``updated_since`` incremental filters, strict rate
limits. A vendor connector can subclass this and only override auth / pagination quirks.

Error classification (plan 5.8): 401/403 -> AuthError, 429 -> TransientError (honours
Retry-After), 5xx / timeouts -> TransientError(maybe_delivered=True), other 4xx -> ContractError
for reads and PermanentError for writes.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ...core.config import Settings, get_settings
from ..errors import AuthError, ContractError, PermanentError, TransientError
from ..retry import RetryPolicy, call_with_retry
from ..spi import Checkpoint, ConnCtx, HealthResult, RawItem, SubmitResult, SyncWindow, WritebackRequest
from ..streams import STREAMS, FieldError, parse_datetime

USER_AGENT = "aegis-predict-connector/1.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaginationConfig(_Model):
    type: Literal["none", "page", "offset", "cursor"] = "none"
    page_param: str = "page"
    size_param: str = "page_size"
    offset_param: str = "offset"
    page_size: int = Field(100, ge=1, le=10_000)
    start_page: int = 1
    cursor_param: str = "cursor"
    next_cursor_path: str = "next_cursor"
    max_pages: int = Field(10_000, ge=1)


class CountConfig(_Model):
    path: str
    count_path: str = "total"
    static_params: dict[str, Any] = Field(default_factory=dict)


class StreamConfig(_Model):
    path: str
    method: Literal["GET", "POST"] = "GET"
    records_path: str = "data"
    # canonical field -> dotted path in the source record (identity when omitted)
    field_map: dict[str, str] = Field(default_factory=dict)
    static_params: dict[str, Any] = Field(default_factory=dict)
    updated_at_field: str | None = None
    updated_since_param: str | None = None
    updated_until_param: str | None = None
    timestamp_format: Literal["iso", "epoch_seconds", "epoch_millis"] = "iso"
    lookback_minutes: int = Field(120, ge=0)
    initial_since: str | None = None
    sorted_by_updated_at: bool = False
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    count: CountConfig | None = None


class AuthConfig(_Model):
    type: Literal["none", "bearer", "api_key_header", "hmac_sha256"] = "none"
    header: str = "X-API-Key"


class RateLimitConfig(_Model):
    rate_per_second: float = Field(2.0, gt=0)
    burst: float = Field(4.0, ge=1)


class WritebackCommandConfig(_Model):
    method: Literal["POST", "PUT"] = "POST"
    path: str
    # target body path -> command payload key ("$idempotency_key" for the key itself)
    body_field_map: dict[str, str]
    static_body: dict[str, Any] = Field(default_factory=dict)
    idempotency_header: str | None = "Idempotency-Key"
    external_ref_path: str = "id"


class LookupConfig(_Model):
    path: str
    query_param: str = "external_ref"
    records_path: str = "data"
    id_field: str = "id"


class WritebackConfig(_Model):
    commands: dict[str, WritebackCommandConfig] = Field(default_factory=dict)
    lookup: LookupConfig | None = None


class ExecutionReadbackConfig(_Model):
    path: str
    query_param: str = "external_ref"
    identity: Literal["external_ref", "plan_line_id"] = "external_ref"
    response_path: str = "data"


class GenericRestConfig(_Model):
    base_url: str
    source_system: str | None = None
    timeout_seconds: float = Field(30.0, gt=0, le=300)
    max_retries: int = Field(5, ge=1, le=20)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    health_path: str | None = None
    naive_timezone: str = "UTC"
    streams: dict[str, StreamConfig]
    writeback: WritebackConfig | None = None
    execution_readback: ExecutionReadbackConfig | None = None

    @field_validator("streams")
    @classmethod
    def _known_streams(cls, value: dict[str, StreamConfig]) -> dict[str, StreamConfig]:
        unknown = sorted(set(value) - set(STREAMS))
        if unknown:
            raise ValueError(f"unknown streams: {', '.join(unknown)}")
        if not value:
            raise ValueError("at least one stream must be configured")
        for name, stream in value.items():
            bad = sorted(set(stream.field_map) - set(STREAMS[name].field_names))
            if bad:
                raise ValueError(f"{name}.field_map has non canonical fields: {', '.join(bad)}")
        return value


# -- helpers ---------------------------------------------------------------------------------


def dig(document: Any, path: str | None) -> Any:
    """Dotted path lookup (``data.items.0.sku``); empty path returns the document itself."""

    if not path:
        return document
    current = document
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _assign(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = document
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _blocked_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def guard_url(url: str, settings: Settings, *, resolve: bool) -> None:
    """SSRF guard for tenant supplied URLs (scheme + private / loopback / metadata addresses).

    Literal IPs are checked at validation time; host names are resolved right before each
    connection and every resolved address must be public.
    """

    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ValueError("base_url must be an http(s) URL")
    if parts.scheme == "http" and not settings.connector_allow_insecure_http:
        raise ValueError("base_url must use https")
    host = parts.hostname
    if not host:
        raise ValueError("base_url has no host")
    if settings.connector_allow_private_network:
        return
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        if _blocked_address(host):
            raise ValueError(f"base_url host {host} is a private or reserved address")
        return
    if host.lower() in ("localhost", "metadata.google.internal") or host.lower().endswith(".localhost"):
        raise ValueError(f"base_url host {host} is not allowed")
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransientError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        if _blocked_address(info[4][0]):
            raise ContractError(f"{host} resolves to a private or reserved address")


def _format_ts(moment: datetime, style: str) -> str | int:
    if style == "epoch_seconds":
        return int(moment.timestamp())
    if style == "epoch_millis":
        return int(moment.timestamp() * 1000)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any, style: str, naive_tz: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if style in ("epoch_seconds", "epoch_millis") or isinstance(value, (int, float)):
            number = float(value)
            if style == "epoch_millis" or number > 1e11:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=UTC)
        return parse_datetime(value, naive_tz)
    except (ValueError, TypeError, OverflowError):
        return None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


class GenericRestConnector:
    kind: ClassVar[str] = "generic_rest"
    version: ClassVar[str] = "1.0"

    # -- configuration -----------------------------------------------------------------------
    def validate_config(self, config: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or get_settings()
        try:
            parsed = GenericRestConfig.model_validate(config)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        guard_url(parsed.base_url, settings, resolve=False)
        parse_datetime("2000-01-01T00:00:00", parsed.naive_timezone)
        return parsed.model_dump(mode="json")

    def streams(self, config: dict[str, Any]) -> list[str]:
        return sorted(config.get("streams") or {})

    def writeback_commands(self, config: dict[str, Any]) -> list[str]:
        return sorted(((config.get("writeback") or {}).get("commands") or {}).keys())

    @staticmethod
    def _config(ctx: ConnCtx) -> GenericRestConfig:
        try:
            return GenericRestConfig.model_validate(ctx.config)
        except ValidationError as exc:
            raise ContractError(f"stored connection config is invalid: {exc}") from exc

    @staticmethod
    def _guard(config: GenericRestConfig, ctx: ConnCtx) -> None:
        try:
            guard_url(config.base_url, ctx.settings, resolve=True)
        except ValueError as exc:
            raise ContractError(str(exc)) from exc

    # -- HTTP --------------------------------------------------------------------------------
    def _client(self, config: GenericRestConfig) -> httpx.Client:
        return httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **config.headers},
            follow_redirects=False,
            trust_env=False,
        )

    def _authorize(self, request: httpx.Request, config: GenericRestConfig, secrets: dict[str, Any]) -> None:
        auth = config.auth
        if auth.type == "bearer":
            token = secrets.get("token")
            if not token:
                raise AuthError("bearer token is not configured")
            request.headers["Authorization"] = f"Bearer {token}"
        elif auth.type == "api_key_header":
            key = secrets.get("api_key")
            if not key:
                raise AuthError("api_key is not configured")
            request.headers[auth.header] = str(key)
        elif auth.type == "hmac_sha256":
            app_key, app_secret = secrets.get("app_key"), secrets.get("app_secret")
            if not app_key or not app_secret:
                raise AuthError("app_key / app_secret are not configured")
            timestamp = str(int(time.time()))
            body_hash = hashlib.sha256(request.content or b"").hexdigest()
            message = "\n".join([request.method, request.url.raw_path.decode("ascii"), timestamp, body_hash])
            request.headers["X-App-Key"] = str(app_key)
            request.headers["X-Timestamp"] = timestamp
            request.headers["X-Signature"] = hmac.new(
                str(app_secret).encode("utf-8"), message.encode("utf-8"), hashlib.sha256
            ).hexdigest()

    def _send(
        self,
        ctx: ConnCtx,
        client: httpx.Client,
        config: GenericRestConfig,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        client_error: type[Exception] = ContractError,
    ) -> httpx.Response:
        endpoint = f"{method} {path}"
        ctx.rate_limiter.acquire(endpoint)
        request = client.build_request(method, path, params=params, json=json_body, headers=headers)
        base = urlsplit(config.base_url)
        target = urlsplit(str(request.url))
        if (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port):
            raise ContractError("connector path must remain on the configured origin")
        guard_url(str(request.url), ctx.settings, resolve=True)
        self._authorize(request, config, ctx.secrets)
        try:
            response = client.send(request)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise TransientError(f"{endpoint}: connection failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise TransientError(f"{endpoint}: timed out: {exc}", maybe_delivered=True) from exc
        except httpx.TransportError as exc:
            raise TransientError(f"{endpoint}: transport error: {exc}", maybe_delivered=True) from exc
        status = response.status_code
        if status in (401, 403):
            raise AuthError(f"{endpoint}: credentials rejected ({status})")
        if status == 429:
            retry_after = _retry_after(response)
            ctx.rate_limiter.penalize(endpoint, retry_after if retry_after is not None else 1.0)
            raise TransientError(f"{endpoint}: rate limited", retry_after=retry_after)
        if status >= 500:
            raise TransientError(f"{endpoint}: server error {status}", maybe_delivered=True)
        if status >= 400:
            error = client_error(f"{endpoint}: rejected with {status}: {response.text[:500]}")
            error.status_code = status  # type: ignore[attr-defined]
            raise error
        return response

    def execution_report(self, ctx, external_ref: str, plan_line_id: str):
        config = GenericRestConfig.model_validate(ctx.config)
        self._guard(config, ctx)
        spec = config.execution_readback
        if spec is None:
            raise ContractError("execution readback is not configured")
        identity = external_ref if spec.identity == "external_ref" else plan_line_id
        with self._client(config) as client:
            result = self._get_json(ctx, client, config, "GET", spec.path, params={spec.query_param: identity})
        result = dig(result, spec.response_path)
        if result is not None and not isinstance(result, dict):
            raise ContractError("execution readback must return one cumulative report or null")
        return result

    def _get_json(self, ctx: ConnCtx, client: httpx.Client, config: GenericRestConfig, method: str, path: str, **kwargs: Any) -> Any:
        def attempt() -> Any:
            response = self._send(ctx, client, config, method, path, **kwargs)
            try:
                return response.json()
            except ValueError as exc:
                raise ContractError(f"{method} {path}: response is not JSON") from exc

        return call_with_retry(
            attempt,
            RetryPolicy(max_attempts=config.max_retries),
            sleep=ctx.sleep,
            retry_maybe_delivered=True,
        )

    # -- health ------------------------------------------------------------------------------
    def check(self, ctx: ConnCtx) -> HealthResult:
        config = self._config(ctx)
        try:
            self._guard(config, ctx)
            with self._client(config) as client:
                if config.health_path:
                    self._get_json(ctx, client, config, "GET", config.health_path)
                else:
                    name, stream = sorted(config.streams.items())[0]
                    params = dict(stream.static_params)
                    if stream.pagination.type in ("page", "offset"):
                        params[stream.pagination.size_param] = 1
                    self._get_json(ctx, client, config, stream.method, stream.path, params=params)
        except (AuthError, TransientError, ContractError, PermanentError, ValueError) as exc:
            return HealthResult(False, str(exc), {"category": getattr(exc, "category", "config")})
        return HealthResult(True, "ok")

    # -- read --------------------------------------------------------------------------------
    def read(
        self,
        ctx: ConnCtx,
        stream: str,
        cursor: dict[str, Any] | None,
        window: SyncWindow | None,
    ) -> Iterator[RawItem | Checkpoint]:
        config = self._config(ctx)
        stream_cfg = config.streams[stream]
        template = STREAMS[stream]
        self._guard(config, ctx)

        previous = _parse_ts((cursor or {}).get("updated_since"), "iso", "UTC")
        if window is not None:
            since, until = window.start, window.end
        else:
            since = previous - timedelta(minutes=stream_cfg.lookback_minutes) if previous else None
            if since is None and stream_cfg.initial_since:
                since = parse_datetime(stream_cfg.initial_since, config.naive_timezone)
            until = None
        base_params = dict(stream_cfg.static_params)
        if stream_cfg.updated_since_param and since is not None:
            base_params[stream_cfg.updated_since_param] = _format_ts(since, stream_cfg.timestamp_format)
        if stream_cfg.updated_until_param and until is not None:
            base_params[stream_cfg.updated_until_param] = _format_ts(until, stream_cfg.timestamp_format)

        field_map = stream_cfg.field_map or {name: name for name in template.field_names}
        high_water = previous
        checked_contract = False
        pagination = stream_cfg.pagination
        page, offset, next_cursor = pagination.start_page, 0, None
        with self._client(config) as client:
            for _ in range(pagination.max_pages):
                params = dict(base_params)
                if pagination.type == "page":
                    params[pagination.page_param] = page
                    params[pagination.size_param] = pagination.page_size
                elif pagination.type == "offset":
                    params[pagination.offset_param] = offset
                    params[pagination.size_param] = pagination.page_size
                elif pagination.type == "cursor" and next_cursor:
                    params[pagination.cursor_param] = next_cursor
                if stream_cfg.method == "GET":
                    body = self._get_json(ctx, client, config, "GET", stream_cfg.path, params=params)
                else:
                    body = self._get_json(ctx, client, config, "POST", stream_cfg.path, json_body=params)
                records = dig(body, stream_cfg.records_path)
                if records is None:
                    raise ContractError(f"{stream}: response has no '{stream_cfg.records_path}'")
                if not isinstance(records, list):
                    raise ContractError(f"{stream}: '{stream_cfg.records_path}' is not a list")
                for record in records:
                    payload = {name: dig(record, source) for name, source in field_map.items()}
                    try:
                        external_id = template.external_id(payload)
                    except FieldError as exc:
                        if not checked_contract:
                            raise ContractError(f"{stream}: first record has no key ({exc})") from exc
                        external_id = "missing-key:" + hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()[:32]
                    checked_contract = True
                    updated = (
                        _parse_ts(dig(record, stream_cfg.updated_at_field), stream_cfg.timestamp_format, config.naive_timezone)
                        if stream_cfg.updated_at_field
                        else None
                    )
                    if updated is not None and (high_water is None or updated > high_water):
                        high_water = updated
                    yield RawItem(stream, external_id, payload, updated, f"{stream_cfg.path}#{page if pagination.type == 'page' else offset}")
                if stream_cfg.sorted_by_updated_at and high_water is not None:
                    yield Checkpoint(stream, {"updated_since": high_water.isoformat()})
                if pagination.type == "none":
                    break
                if pagination.type == "cursor":
                    next_cursor = dig(body, pagination.next_cursor_path)
                    if not next_cursor or not records:
                        break
                else:
                    if len(records) < pagination.page_size:
                        break
                    page += 1
                    offset += len(records)
            else:
                raise ContractError(f"{stream}: pagination exceeded max_pages={pagination.max_pages}")
        if high_water is not None:
            yield Checkpoint(stream, {"updated_since": high_water.isoformat()})

    def count(self, ctx: ConnCtx, stream: str, window: SyncWindow) -> int | None:
        config = self._config(ctx)
        stream_cfg = config.streams.get(stream)
        if stream_cfg is None or stream_cfg.count is None:
            return None
        params = dict(stream_cfg.count.static_params)
        if stream_cfg.updated_since_param:
            params[stream_cfg.updated_since_param] = _format_ts(window.start, stream_cfg.timestamp_format)
        if stream_cfg.updated_until_param:
            params[stream_cfg.updated_until_param] = _format_ts(window.end, stream_cfg.timestamp_format)
        self._guard(config, ctx)
        with self._client(config) as client:
            body = self._get_json(ctx, client, config, "GET", stream_cfg.count.path, params=params)
        value = dig(body, stream_cfg.count.count_path)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{stream}: count response has no integer at '{stream_cfg.count.count_path}'") from exc

    # -- writeback ---------------------------------------------------------------------------
    def submit(self, ctx: ConnCtx, request: WritebackRequest) -> SubmitResult:
        config = self._config(ctx)
        if config.writeback is None or request.command_type not in config.writeback.commands:
            raise PermanentError(f"command {request.command_type} is not configured for this connection")
        command = config.writeback.commands[request.command_type]
        body: dict[str, Any] = dict(command.static_body)
        for target, source in command.body_field_map.items():
            value = request.idempotency_key if source == "$idempotency_key" else request.payload.get(source)
            _assign(body, target, value)
        headers = {command.idempotency_header: request.idempotency_key} if command.idempotency_header else None
        self._guard(config, ctx)

        def attempt() -> httpx.Response:
            return self._send(
                ctx, client, config, command.method, command.path, json_body=body, headers=headers, client_error=PermanentError
            )

        with self._client(config) as client:
            try:
                # Only failures that certainly did not reach the server are retried in place;
                # anything ambiguous goes back to the state machine for a lookup first.
                response = call_with_retry(
                    attempt, RetryPolicy(max_attempts=config.max_retries), sleep=ctx.sleep, retry_maybe_delivered=False
                )
            except PermanentError as exc:
                # Many ERPs answer 409 for a duplicate idempotency key: the document already exists.
                if getattr(exc, "status_code", None) == 409:
                    existing = self.lookup(ctx, request)
                    if existing:
                        return SubmitResult(existing)
                raise
        try:
            document = response.json() if response.content else {}
        except ValueError:
            document = {}
        reference = dig(document, command.external_ref_path)
        if reference in (None, ""):
            raise TransientError("create response has no external reference; lookup required", maybe_delivered=True)
        return SubmitResult(str(reference))

    def lookup(self, ctx: ConnCtx, request: WritebackRequest) -> str | None:
        config = self._config(ctx)
        if config.writeback is None or config.writeback.lookup is None:
            raise PermanentError("lookup is not configured; ambiguous writebacks cannot be resolved automatically")
        lookup = config.writeback.lookup
        self._guard(config, ctx)
        with self._client(config) as client:
            body = self._get_json(
                ctx, client, config, "GET", lookup.path, params={lookup.query_param: request.idempotency_key}
            )
        records = dig(body, lookup.records_path)
        if isinstance(records, dict):
            records = [records]
        if not records:
            return None
        reference = dig(records[0], lookup.id_field)
        return str(reference) if reference not in (None, "") else None
