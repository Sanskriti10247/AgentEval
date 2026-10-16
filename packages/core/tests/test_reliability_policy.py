import pytest

from agenteval_core.reliability.policy import (
    MAX_UNSTARTED_CLAIMS,
    DeadLetterReason,
    ErrorKind,
    RunDecision,
    after_claim_expired,
    after_completed_attempt,
    after_evaluation_error,
    after_evaluation_shutdown,
    after_failed_attempt,
    after_running_expired,
    check_budget,
    replay_budget,
)
from agenteval_core.reliability.taxonomy import EvaluationErrorReason, FailureReason
from agenteval_core.reliability.transitions import (
    EVALUATION_TRANSITIONS,
    RUN_TRANSITIONS,
    EvaluationState,
    RunState,
)

_RUN_CODES = {t.code: t for t in RUN_TRANSITIONS}
_EVAL_CODES = {t.code: t for t in EVALUATION_TRANSITIONS}


def _failed(
    reason: FailureReason, count: int, budget: int, *, cancel: bool = False, rec: bool = False
) -> RunDecision:
    return after_failed_attempt(
        reason, attempt_count=count, max_attempts=budget, cancel_requested=cancel, by_reconciler=rec
    )


def test_retryable_failure_with_budget_left_retries() -> None:
    d = _failed(FailureReason.MODEL_SERVER_ERROR, 1, 3)
    assert (d.code, d.status, d.dead_letter_reason) == ("R8", RunState.RETRYING, None)
    assert _failed(FailureReason.MODEL_SERVER_ERROR, 1, 3, rec=True).code == "R9"


def test_retryable_failure_at_the_budget_dead_letters_as_exhausted() -> None:
    d = _failed(FailureReason.MODEL_SERVER_ERROR, 3, 3)
    assert (d.code, d.status, d.dead_letter_reason) == (
        "R10",
        RunState.FAILED,
        DeadLetterReason.RETRIES_EXHAUSTED,
    )


def test_non_retryable_failure_dead_letters_immediately() -> None:
    d = _failed(FailureReason.MODEL_AUTHENTICATION, 1, 3)
    assert (d.status, d.dead_letter_reason) == (RunState.FAILED, DeadLetterReason.NON_RETRYABLE)


@pytest.mark.parametrize(
    "reason", [FailureReason.MODEL_SERVER_ERROR, FailureReason.MODEL_AUTHENTICATION]
)
@pytest.mark.parametrize("count", [1, 3])
def test_a_pending_cancel_wins_over_every_failure(reason: FailureReason, count: int) -> None:
    d = _failed(reason, count, 3, cancel=True)
    assert (d.code, d.status, d.dead_letter_reason) == ("R11", RunState.CANCELLED, None)


def test_completed_outcomes_are_never_retried_but_lose_to_a_cancel() -> None:
    assert after_completed_attempt(cancel_requested=False).status is RunState.COMPLETED
    assert after_completed_attempt(cancel_requested=True).status is RunState.CANCELLED


def test_running_lease_expiry_uses_the_lease_expired_reason() -> None:
    assert (
        after_running_expired(attempt_count=1, max_attempts=2, cancel_requested=False).code == "R9"
    )
    d = after_running_expired(attempt_count=2, max_attempts=2, cancel_requested=False)
    assert (d.code, d.dead_letter_reason) == ("R10", DeadLetterReason.RETRIES_EXHAUSTED)
    assert (
        after_running_expired(attempt_count=1, max_attempts=2, cancel_requested=True).code == "R11"
    )


def test_repeated_claim_expiry_fails_at_the_fixed_limit() -> None:
    codes = [
        after_claim_expired(unstarted_claims=n, cancel_requested=False).code
        for n in range(MAX_UNSTARTED_CLAIMS)
    ]
    assert codes == ["R4a", "R4a", "R4b"]
    last = after_claim_expired(unstarted_claims=MAX_UNSTARTED_CLAIMS - 1, cancel_requested=False)
    assert last.failure_reason is FailureReason.CLAIM_ABANDONED
    assert last.dead_letter_reason is DeadLetterReason.RETRIES_EXHAUSTED
    assert after_claim_expired(unstarted_claims=2, cancel_requested=True).code == "R5"


def test_every_run_decision_is_a_listed_transition() -> None:
    decisions = [
        _failed(FailureReason.MODEL_TIMEOUT, 1, 3),
        _failed(FailureReason.MODEL_TIMEOUT, 1, 3, rec=True),
        _failed(FailureReason.MODEL_TIMEOUT, 3, 3),
        _failed(FailureReason.MODEL_TIMEOUT, 1, 3, cancel=True),
        after_completed_attempt(cancel_requested=False),
        after_claim_expired(unstarted_claims=0, cancel_requested=False),
        after_claim_expired(unstarted_claims=2, cancel_requested=False),
        after_claim_expired(unstarted_claims=0, cancel_requested=True),
    ]
    for d in decisions:
        assert _RUN_CODES[d.code].dst is d.status, d


@pytest.mark.parametrize(("value", "ok"), [(0, False), (1, True), (10, True), (11, False)])
def test_budget_bounds(value: int, ok: bool) -> None:
    if ok:
        assert check_budget(value) == value
    else:
        with pytest.raises(ValueError):
            check_budget(value)


def test_replay_budget() -> None:
    assert replay_budget(3, 1) == 4
    assert replay_budget(1, 9) == 10
    for bad in ((3, 0), (3, 10), (5, 6)):
        with pytest.raises(ValueError):
            replay_budget(*bad)


def test_retryable_evaluation_error_with_budget_left_goes_back_to_pending() -> None:
    d = after_evaluation_error(
        EvaluationErrorReason.SANDBOX_FAILED, eval_attempts=1, max_eval_attempts=3
    )
    assert (d.code, d.status, d.error_kind) == ("E7", EvaluationState.PENDING, None)
    r = after_evaluation_error(
        EvaluationErrorReason.LEASE_EXPIRED,
        eval_attempts=1,
        max_eval_attempts=3,
        by_reconciler=True,
    )
    assert r.code == "E9a"


def test_evaluation_budget_exhaustion_and_non_retryable_reasons_end_in_error() -> None:
    d = after_evaluation_error(
        EvaluationErrorReason.SANDBOX_FAILED, eval_attempts=3, max_eval_attempts=3
    )
    assert (d.code, d.error_kind) == ("E6", ErrorKind.RETRIES_EXHAUSTED)
    d = after_evaluation_error(
        EvaluationErrorReason.CAPTURE_PERSIST_FAILED, eval_attempts=1, max_eval_attempts=3
    )
    assert (d.code, d.error_kind) == ("E6", ErrorKind.NON_RETRYABLE)
    d = after_evaluation_error(
        EvaluationErrorReason.LEASE_EXPIRED,
        eval_attempts=3,
        max_eval_attempts=3,
        by_reconciler=True,
    )
    assert (d.code, d.error_kind) == ("E9b", ErrorKind.RETRIES_EXHAUSTED)


def test_shutdown_release_is_budget_aware() -> None:
    assert after_evaluation_shutdown(eval_attempts=2, max_eval_attempts=3).code == "E8a"
    d = after_evaluation_shutdown(eval_attempts=3, max_eval_attempts=3)
    assert (d.code, d.status, d.error_kind) == (
        "E8b",
        EvaluationState.ERROR,
        ErrorKind.RETRIES_EXHAUSTED,
    )


@pytest.mark.parametrize("budget", range(1, 11))
@pytest.mark.parametrize("reason", list(EvaluationErrorReason))
def test_no_decision_ever_produces_an_exhausted_pending_row(
    budget: int, reason: EvaluationErrorReason
) -> None:
    for used in range(1, budget + 1):
        for rec in (False, True):
            d = after_evaluation_error(
                reason, eval_attempts=used, max_eval_attempts=budget, by_reconciler=rec
            )
            assert _EVAL_CODES[d.code].dst is d.status
            if d.status is EvaluationState.PENDING:
                assert used < budget
        s = after_evaluation_shutdown(eval_attempts=used, max_eval_attempts=budget)
        if s.status is EvaluationState.PENDING:
            assert used < budget
