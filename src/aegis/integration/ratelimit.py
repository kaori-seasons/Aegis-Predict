from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    rate: float
    capacity: float
    tokens: float
    updated: float


class RateLimiter:
    """In-process token bucket keyed by endpoint.

    The connection lease guarantees a single worker per connection, so a process-local bucket is
    enough to respect per-connection API quotas without Redis.
    """

    def __init__(
        self,
        rate_per_second: float = 5.0,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(rate_per_second, 1e-6)
        self.burst = max(burst if burst is not None else rate_per_second, 1.0)
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._penalty_until: dict[str, float] = {}

    def _bucket(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(self.rate, self.burst, self.burst, self._clock())
            self._buckets[key] = bucket
        return bucket

    def configure(self, key: str, rate: float, burst: float) -> None:
        """Endpoint-specific platform quotas without resetting an existing token balance."""
        with self._lock:
            existing = self._buckets.get(key)
            if existing is None:
                self._buckets[key] = _Bucket(rate, burst, burst, self._clock())
            else:
                existing.rate, existing.capacity = rate, burst
                existing.tokens = min(existing.tokens, burst)

    def acquire(self, key: str = "default", tokens: float = 1.0) -> float:
        """Block until a token is available; returns the time waited (seconds)."""

        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                penalty = self._penalty_until.get(key, 0.0)
                if penalty > now:
                    delay = penalty - now
                else:
                    bucket = self._bucket(key)
                    bucket.tokens = min(bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.rate)
                    bucket.updated = now
                    if bucket.tokens >= tokens:
                        bucket.tokens -= tokens
                        return waited
                    delay = (tokens - bucket.tokens) / bucket.rate
            self._sleep(delay)
            waited += delay

    def penalize(self, key: str, seconds: float) -> None:
        """Honour ``Retry-After``: nobody uses this endpoint until the server-provided time passes."""

        with self._lock:
            until = self._clock() + max(0.0, seconds)
            self._penalty_until[key] = max(self._penalty_until.get(key, 0.0), until)
