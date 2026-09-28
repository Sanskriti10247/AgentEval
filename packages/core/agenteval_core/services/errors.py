class ServiceError(Exception):
    """Base class for use-case errors that the API maps to HTTP responses."""


class NotFound(ServiceError):
    pass


class InvalidTask(ServiceError):
    pass


class IdempotencyConflict(ServiceError):
    """The Idempotency-Key was already used for a different request."""


class DispatchUnavailable(ServiceError):
    """The run is persisted as QUEUED but could not be put on the queue."""

    def __init__(self, run_id: object) -> None:
        super().__init__(
            f"run {run_id} is queued but dispatch failed; retry with the same Idempotency-Key"
        )
        self.run_id = run_id


class InvalidAgentVersion(ServiceError):
    """The agent version config is invalid or not runnable here."""


class InvalidRun(ServiceError):
    """The run request is inconsistent (e.g. agent task without an agent version)."""


class Conflict(ServiceError):
    """A uniqueness rule was violated (e.g. agent name taken)."""
