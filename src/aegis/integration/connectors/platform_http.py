"""Shared bounded, read-only transport. Errors never include credential-bearing URLs / bodies."""
from __future__ import annotations

from typing import Any
import httpx
from ..errors import AuthError, ContractError, TransientError
from ..retry import RetryPolicy, call_with_retry
from .generic_rest import _retry_after


def send_json(client: httpx.Client, ctx, method: str, url: str, *, endpoint: str, **kwargs: Any) -> dict:
    def attempt():
        ctx.rate_limiter.acquire(endpoint)
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise TransientError(f"{endpoint}: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise AuthError(f"{endpoint}: credentials rejected")
        if response.status_code == 429 or response.status_code >= 500:
            delay = _retry_after(response)
            ctx.rate_limiter.penalize(endpoint, delay or 1)
            raise TransientError(f"{endpoint}: HTTP {response.status_code}", retry_after=delay)
        if response.status_code != 200:
            raise ContractError(f"{endpoint}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ContractError(f"{endpoint}: invalid JSON") from exc
        if not isinstance(data, dict):
            raise ContractError(f"{endpoint}: expected object")
        return data
    return call_with_retry(attempt, RetryPolicy(max_attempts=3), sleep=ctx.sleep)
