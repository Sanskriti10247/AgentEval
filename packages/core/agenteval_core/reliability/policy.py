"""Retry, recovery and budget decisions (F6, F9, G5, G7, G9).

Each function answers "which transition applies?" for one situation. They are
pure; the repositories apply the answer with a guarded UPDATE. Cancellation
always takes precedence over every other outcome.
"""

from dataclasses import dataclass
from enum import StrEnum

from agenteval_core.reliability.taxonomy import (
    EvaluationErrorReason,
    FailureReason,
    is_evaluation_retryable,
    is_retryable,
)
from agenteval_core.reliability.transitions import EvaluationState, RunState

MAX_ATTEMPTS_CAP = 10  # execution and grading budgets are both 1..10
MAX_UNSTARTED_CLAIMS = 3  # G5: a fixed constant, deliberately not a setting
REPLAY_MAX_ADDITIONAL = 9

# Namespace of the reconciler's per-step advisory locks: pg_try_advisory_xact_lock(NS, step).
RECONCILER_LOCK_NS = 0x41455650  # "AEVP"


class DeadLetterReason(StrEnum):
    RETRIES_EXHAUSTED = "retries_exhausted"
    NON_RETRYABLE = "non_retryable"


class ErrorKind(StrEnum):
    NON_RETRYABLE = "non_retryable"
    RETRIES_EXHAUSTED = "retries_exhausted"


@dataclass(frozen=True)
class RunDecision:
    code: str
    status: RunState
    dead_letter_reason: DeadLetterReason | None = None
    failure_reason: FailureReason | None = None  # run-level reason (R4b)


@dataclass(frozen=True)
class EvaluationDecision:
    code: str
    status: EvaluationState
    error_kind: ErrorKind | None = None


def check_budget(value: int) -> int:
    if not 1 <= value <= MAX_ATTEMPTS_CAP:
        raise ValueError(f"attempt budget must be between 1 and {MAX_ATTEMPTS_CAP}")
    return value


def after_failed_attempt(
    reason: FailureReason,
    *,
    attempt_count: int,
    max_attempts: int,
    cancel_requested: bool,
    by_reconciler: bool,
) -> RunDecision:
    """A RUNNING run whose attempt failed (FINISHED with a failed outcome, or
    ABANDONED after lease expiry when `by_reconciler`)."""
    if cancel_requested:
        return RunDecision("R11", RunState.CANCELLED)
    if is_retryable(reason) and attempt_count < max_attempts:
        return RunDecision("R9" if by_reconciler else "R8", RunState.RETRYING)
    dead = (
        DeadLetterReason.RETRIES_EXHAUSTED
        if is_retryable(reason)
        else DeadLetterReason.NON_RETRYABLE
    )
    return RunDecision("R10", RunState.FAILED, dead_letter_reason=dead)


def after_completed_attempt(*, cancel_requested: bool) -> RunDecision:
    """A completed outcome is never retried; a pending cancel still wins (G7)."""
    if cancel_requested:
        return RunDecision("R11", RunState.CANCELLED)
    return RunDecision("R7", RunState.COMPLETED)


def after_claim_expired(*, unstarted_claims: int, cancel_requested: bool) -> RunDecision:
    """A CLAIMED run whose lease expired before any attempt started (R5 / R4a / R4b)."""
    if cancel_requested:
        return RunDecision("R5", RunState.CANCELLED)
    if unstarted_claims + 1 < MAX_UNSTARTED_CLAIMS:
        return RunDecision("R4a", RunState.QUEUED)
    return RunDecision(
        "R4b",
        RunState.FAILED,
        dead_letter_reason=DeadLetterReason.RETRIES_EXHAUSTED,
        failure_reason=FailureReason.CLAIM_ABANDONED,
    )


def after_running_expired(
    *, attempt_count: int, max_attempts: int, cancel_requested: bool
) -> RunDecision:
    """A RUNNING run whose lease expired: the attempt becomes ABANDONED (A3)."""
    return after_failed_attempt(
        FailureReason.LEASE_EXPIRED,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        cancel_requested=cancel_requested,
        by_reconciler=True,
    )


def replay_budget(max_attempts: int, additional: int) -> int:
    """R14: the new max_attempts, or ValueError (the API answers 422)."""
    if not 1 <= additional <= REPLAY_MAX_ADDITIONAL:
        raise ValueError(f"additional_attempts must be between 1 and {REPLAY_MAX_ADDITIONAL}")
    if max_attempts + additional > MAX_ATTEMPTS_CAP:
        raise ValueError(f"max_attempts would exceed {MAX_ATTEMPTS_CAP}")
    return max_attempts + additional


def after_evaluation_error(
    reason: EvaluationErrorReason,
    *,
    eval_attempts: int,
    max_eval_attempts: int,
    by_reconciler: bool = False,
) -> EvaluationDecision:
    """A RUNNING evaluation that failed (E6/E7) or whose lease expired (E9a/E9b).
    Every transition to PENDING requires budget left, so no PENDING row is ever
    exhausted (G9)."""
    budget_left = eval_attempts < max_eval_attempts
    if is_evaluation_retryable(reason) and budget_left:
        return EvaluationDecision("E9a" if by_reconciler else "E7", EvaluationState.PENDING)
    kind = (
        ErrorKind.RETRIES_EXHAUSTED if is_evaluation_retryable(reason) else ErrorKind.NON_RETRYABLE
    )
    return EvaluationDecision(
        "E9b" if by_reconciler else "E6", EvaluationState.ERROR, error_kind=kind
    )


def after_evaluation_shutdown(*, eval_attempts: int, max_eval_attempts: int) -> EvaluationDecision:
    """Shutdown release (E8a/E8b). The claim's attempt is consumed, never refunded."""
    if eval_attempts < max_eval_attempts:
        return EvaluationDecision("E8a", EvaluationState.PENDING)
    return EvaluationDecision("E8b", EvaluationState.ERROR, error_kind=ErrorKind.RETRIES_EXHAUSTED)
