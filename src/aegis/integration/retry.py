from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .errors import TransientError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 60.0

    def backoff(self, attempt: int, rng: random.Random | None = None) -> float:
        """Exponential backoff with full jitter (attempt is 1-based)."""

        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return (rng or random).uniform(0, ceiling)


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, TransientError, float], None] | None = None,
    retry_maybe_delivered: bool = True,
) -> T:
    """Retry ``TransientError`` only. Never use ``retry_maybe_delivered=True`` for non-idempotent creates."""

    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except TransientError as exc:
            if attempt >= policy.max_attempts or (exc.maybe_delivered and not retry_maybe_delivered):
                raise
            delay = exc.retry_after if exc.retry_after is not None else policy.backoff(attempt)
            delay = min(delay, policy.max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
