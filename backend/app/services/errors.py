"""Service-layer errors. `http_status` is only a hint for the API layer (this package never touches HTTP)."""
from __future__ import annotations


class ServiceError(Exception):
    http_status = 500
    code = "internal_error"

    def __init__(self, message: str, *, details=None, code: str | None = None, headers: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details if details is not None else []
        if code:
            self.code = code
        self.headers = headers or {}


class Unauthorized(ServiceError):
    http_status, code = 401, "unauthorized"


class NotFound(ServiceError):
    http_status, code = 404, "not_found"


class ValidationFailed(ServiceError):
    """details = list of {field, code, message} issues (see validation.Issue)."""
    http_status, code = 422, "validation_failed"


class Conflict(ServiceError):
    http_status, code = 409, "conflict"


class DuplicateActiveExperiment(Conflict):
    code = "duplicate_active_experiment"


class IdempotencyKeyReused(Conflict):
    code = "idempotency_key_reused"


class NotCancellable(Conflict):
    code = "not_cancellable"


class StillActive(Conflict):
    code = "experiment_active"


class ResultsNotAvailable(Conflict):
    code = "results_not_available"


class QueueFull(ServiceError):
    http_status, code = 429, "queue_full"


class ServiceUnavailable(ServiceError):
    http_status, code = 503, "service_unavailable"


class ConfigurationError(Exception):
    """Bad server configuration (environment variables); raised at start-up, never per request."""
