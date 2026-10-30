"""Closed failure taxonomy (F4).

Every failure is a reason from a fixed enum. Retryability and the outcome a
reason must pair with come from one policy table; the category is derived from
the reason and never stored. Free-text error messages never drive logic.

Database CHECK lists are generated from these enums, so the schema and the
code cannot drift apart.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from agenteval_core.providers.base import ModelErrorKind
from agenteval_core.states import FAILED_OUTCOMES, Outcome


class FailureReason(StrEnum):
    # Model, transient
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_OVERLOADED = "model_overloaded"
    MODEL_SERVER_ERROR = "model_server_error"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_CONNECTION = "model_connection"
    MODEL_CONFLICT = "model_conflict"
    # Model, permanent
    MODEL_BAD_REQUEST = "model_bad_request"
    MODEL_AUTHENTICATION = "model_authentication"
    MODEL_PERMISSION = "model_permission"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_REQUEST_TOO_LARGE = "model_request_too_large"
    MODEL_PROTOCOL = "model_protocol"  # an unexpected stop reason
    # Provider and configuration
    CONFIG_PROVIDER_DISABLED = "config_provider_disabled"
    CONFIG_MODEL_UNPRICED = "config_model_unpriced"
    CONFIG_INVALID = "config_invalid"
    # The run's evaluation had already spent its max_cost_usd: the model was never called.
    EVALUATION_BUDGET_EXHAUSTED = "evaluation_budget_exhausted"
    # Sandbox
    SANDBOX_IMAGE_NOT_FOUND = "sandbox_image_not_found"
    SANDBOX_CREATE_FAILED = "sandbox_create_failed"
    SANDBOX_EXEC_FAILED = "sandbox_exec_failed"
    SANDBOX_REAPER_FAILED = "sandbox_reaper_failed"
    SANDBOX_LOST = "sandbox_lost"
    # Worker and internal
    WORKER_CAPABILITY = "worker_capability"
    WORKER_BACKSTOP = "worker_backstop"
    INTERNAL_UNEXPECTED = "internal_unexpected"
    PERSISTENCE_TRAJECTORY = "persistence_trajectory"
    WORKER_SHUTDOWN = "worker_shutdown"
    # Recovery and cancellation (attempt level)
    LEASE_EXPIRED = "lease_expired"  # A3 only
    CANCELLED_BY_USER = "cancelled_by_user"  # A4 only
    CANCELLED_BY_BATCH = "cancelled_by_batch"  # A4 only
    # Run level only: the run failed before any attempt existed
    INTERNAL_INCONSISTENT = "internal_inconsistent"  # R6
    CLAIM_ABANDONED = "claim_abandoned"  # R4b
    # Backfill of pre-Phase-5 rows
    UNCLASSIFIED = "unclassified"


class ReasonCategory(StrEnum):
    MODEL_TRANSIENT = "model_transient"
    MODEL_PERMANENT = "model_permanent"
    CONFIG = "config"
    SANDBOX = "sandbox"
    WORKER = "worker"
    INTERNAL = "internal"
    PERSISTENCE = "persistence"
    SHUTDOWN = "shutdown"
    LEASE = "lease"
    CANCELLATION = "cancellation"
    LEGACY = "legacy"


@dataclass(frozen=True)
class ReasonPolicy:
    category: ReasonCategory
    retryable: bool
    # Outcomes the reason may be stored with; empty means the outcome must be NULL.
    outcomes: frozenset[Outcome]


_MODEL = frozenset({Outcome.MODEL_ERROR})
_SANDBOX = frozenset({Outcome.SANDBOX_ERROR})
_INTERNAL = frozenset({Outcome.INTERNAL_ERROR})
_NONE: frozenset[Outcome] = frozenset()

_C = ReasonCategory
_R = FailureReason

POLICY: dict[FailureReason, ReasonPolicy] = {
    _R.MODEL_RATE_LIMITED: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_OVERLOADED: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_SERVER_ERROR: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_TIMEOUT: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_CONNECTION: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_CONFLICT: ReasonPolicy(_C.MODEL_TRANSIENT, True, _MODEL),
    _R.MODEL_BAD_REQUEST: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.MODEL_AUTHENTICATION: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.MODEL_PERMISSION: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.MODEL_NOT_FOUND: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.MODEL_REQUEST_TOO_LARGE: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.MODEL_PROTOCOL: ReasonPolicy(_C.MODEL_PERMANENT, False, _MODEL),
    _R.CONFIG_PROVIDER_DISABLED: ReasonPolicy(_C.CONFIG, False, _INTERNAL),
    _R.CONFIG_MODEL_UNPRICED: ReasonPolicy(_C.CONFIG, False, _INTERNAL),
    _R.CONFIG_INVALID: ReasonPolicy(_C.CONFIG, False, _INTERNAL),
    _R.EVALUATION_BUDGET_EXHAUSTED: ReasonPolicy(_C.CONFIG, False, _INTERNAL),
    _R.SANDBOX_IMAGE_NOT_FOUND: ReasonPolicy(_C.SANDBOX, False, _SANDBOX),
    _R.SANDBOX_CREATE_FAILED: ReasonPolicy(_C.SANDBOX, True, _SANDBOX),
    _R.SANDBOX_EXEC_FAILED: ReasonPolicy(_C.SANDBOX, True, _SANDBOX),
    _R.SANDBOX_REAPER_FAILED: ReasonPolicy(_C.SANDBOX, True, _SANDBOX),
    _R.SANDBOX_LOST: ReasonPolicy(_C.SANDBOX, True, _SANDBOX),
    _R.WORKER_CAPABILITY: ReasonPolicy(_C.WORKER, True, _INTERNAL),
    _R.WORKER_BACKSTOP: ReasonPolicy(_C.WORKER, True, _INTERNAL),
    _R.INTERNAL_UNEXPECTED: ReasonPolicy(_C.INTERNAL, True, _INTERNAL),
    _R.PERSISTENCE_TRAJECTORY: ReasonPolicy(_C.PERSISTENCE, True, _INTERNAL),
    _R.WORKER_SHUTDOWN: ReasonPolicy(_C.SHUTDOWN, True, _INTERNAL),
    _R.LEASE_EXPIRED: ReasonPolicy(_C.LEASE, True, _INTERNAL),
    _R.CANCELLED_BY_USER: ReasonPolicy(_C.CANCELLATION, False, _NONE),
    _R.CANCELLED_BY_BATCH: ReasonPolicy(_C.CANCELLATION, False, _NONE),
    _R.INTERNAL_INCONSISTENT: ReasonPolicy(_C.INTERNAL, False, _INTERNAL),
    _R.CLAIM_ABANDONED: ReasonPolicy(_C.INTERNAL, False, _INTERNAL),
    _R.UNCLASSIFIED: ReasonPolicy(_C.LEGACY, False, FAILED_OUTCOMES),
}

# Separate CHECK lists from one enum (G12): an attempt can never carry a
# run-level reason, and a run carries only the run-level ones.
RUN_LEVEL_REASONS: frozenset[FailureReason] = frozenset(
    {_R.INTERNAL_INCONSISTENT, _R.CLAIM_ABANDONED}
)
ATTEMPT_REASONS: frozenset[FailureReason] = frozenset(FailureReason) - RUN_LEVEL_REASONS


def category_of(reason: FailureReason) -> ReasonCategory:
    return POLICY[reason].category


def is_retryable(reason: FailureReason) -> bool:
    return POLICY[reason].retryable


def outcome_allowed(reason: FailureReason, outcome: Outcome | None) -> bool:
    """Whether `reason` may be stored next to `outcome` (None = NULL outcome)."""
    allowed = POLICY[reason].outcomes
    return outcome is None if not allowed else outcome in allowed


_MODEL_KIND_REASON: dict[ModelErrorKind, FailureReason] = {
    ModelErrorKind.RATE_LIMITED: _R.MODEL_RATE_LIMITED,
    ModelErrorKind.OVERLOADED: _R.MODEL_OVERLOADED,
    ModelErrorKind.SERVER_ERROR: _R.MODEL_SERVER_ERROR,
    ModelErrorKind.TIMEOUT: _R.MODEL_TIMEOUT,
    ModelErrorKind.CONNECTION: _R.MODEL_CONNECTION,
    ModelErrorKind.CONFLICT: _R.MODEL_CONFLICT,
    ModelErrorKind.BAD_REQUEST: _R.MODEL_BAD_REQUEST,
    ModelErrorKind.AUTHENTICATION: _R.MODEL_AUTHENTICATION,
    ModelErrorKind.PERMISSION: _R.MODEL_PERMISSION,
    ModelErrorKind.NOT_FOUND: _R.MODEL_NOT_FOUND,
    ModelErrorKind.REQUEST_TOO_LARGE: _R.MODEL_REQUEST_TOO_LARGE,
}


def reason_for_model_error(kind: ModelErrorKind) -> FailureReason:
    return _MODEL_KIND_REASON[kind]


class CancelReason(StrEnum):
    USER = "user"
    BATCH = "batch"  # runs only; a batch itself is always cancelled by a user


class WorkspaceCapture(StrEnum):
    """What happened to the workspace snapshot of a FINISHED, completed attempt (O3d)."""

    NOT_NEEDED = "not_needed"
    STORED = "stored"
    TOO_LARGE = "too_large"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"
    PERSIST_FAILED = "persist_failed"


class EvaluationErrorReason(StrEnum):
    # Not retryable: grading again cannot change the result
    SPEC_INVALID = "spec_invalid"
    SCORING_CONFIG_INVALID = "scoring_config_invalid"
    CAPTURE_TOO_LARGE = "capture_too_large"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    CAPTURE_INVALID = "capture_invalid"
    CAPTURE_PERSIST_FAILED = "capture_persist_failed"
    CAPTURE_MISSING = "capture_missing"
    ARTIFACT_MISSING = "artifact_missing"  # the row exists but its file does not (ADR-008)
    ARTIFACT_CORRUPT = "artifact_corrupt"  # size or SHA-256 mismatch (ADR-008)
    CHECK_ERROR = "check_error"
    # Retryable
    SANDBOX_FAILED = "sandbox_failed"
    BUDGET_TIMEOUT = "budget_timeout"
    INTERNAL_UNEXPECTED = "internal_unexpected"
    WORKER_SHUTDOWN = "worker_shutdown"
    LEASE_EXPIRED = "lease_expired"
    # Backfill
    UNCLASSIFIED = "unclassified"


EVALUATION_RETRYABLE: frozenset[EvaluationErrorReason] = frozenset(
    {
        EvaluationErrorReason.SANDBOX_FAILED,
        EvaluationErrorReason.BUDGET_TIMEOUT,
        EvaluationErrorReason.INTERNAL_UNEXPECTED,
        EvaluationErrorReason.WORKER_SHUTDOWN,
        EvaluationErrorReason.LEASE_EXPIRED,
    }
)


def is_evaluation_retryable(reason: EvaluationErrorReason) -> bool:
    return reason in EVALUATION_RETRYABLE


def sql_values(values: Iterable[StrEnum]) -> str:
    """Render enum values as a sorted SQL IN-list body, e.g. "'a','b'"."""
    return ",".join(f"'{v.value}'" for v in sorted(values, key=lambda v: v.value))


def attempt_reason_check() -> str:
    return f"failure_reason IS NULL OR failure_reason IN ({sql_values(ATTEMPT_REASONS)})"


def run_reason_check() -> str:
    return f"failure_reason IS NULL OR failure_reason IN ({sql_values(RUN_LEVEL_REASONS)})"


def evaluation_reason_check() -> str:
    return f"error_reason IS NULL OR error_reason IN ({sql_values(EvaluationErrorReason)})"
