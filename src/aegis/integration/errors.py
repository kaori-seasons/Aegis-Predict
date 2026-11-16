from __future__ import annotations


class IntegrationError(Exception):
    """Base class; the runtime decides how to react based on the subclass (see plan 5.8)."""

    category = "error"


class TransientError(IntegrationError):
    """429 / 5xx / timeouts: retry with backoff. ``maybe_delivered`` marks requests that may have reached the server."""

    category = "transient"

    def __init__(self, message: str, *, retry_after: float | None = None, maybe_delivered: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.maybe_delivered = maybe_delivered


class AuthError(IntegrationError):
    """Credentials rejected: pause the connection (status ``auth_failed``) and alert."""

    category = "auth"


class DataError(IntegrationError):
    """A single record is invalid: quarantine it, keep the batch going."""

    category = "data"


class ContractError(IntegrationError):
    """The source broke its contract (schema change, missing required field): halt the stream."""

    category = "contract"


class PermanentError(IntegrationError):
    """The remote rejected the request for good (4xx other than auth / rate limit)."""

    category = "permanent"


class LeaseLost(IntegrationError):
    """Another worker took over the connection; stop without touching state (fencing)."""

    category = "lease"
