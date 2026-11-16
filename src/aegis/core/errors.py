from __future__ import annotations


class AegisError(Exception):
    """Base class for domain errors that the API maps to HTTP responses."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(AegisError):
    status_code = 404
    code = "not_found"


class Conflict(AegisError):
    status_code = 409
    code = "conflict"


class PermissionDenied(AegisError):
    status_code = 403
    code = "forbidden"


class Unauthenticated(AegisError):
    status_code = 401
    code = "unauthenticated"


class InvalidInput(AegisError):
    status_code = 422
    code = "invalid_input"


class ConfigurationError(AegisError):
    status_code = 500
    code = "configuration_error"
