import pytest

from agenteval_core.providers.base import RETRYABLE_KINDS, ModelErrorKind
from agenteval_core.reliability.taxonomy import (
    ATTEMPT_REASONS,
    EVALUATION_RETRYABLE,
    POLICY,
    RUN_LEVEL_REASONS,
    EvaluationErrorReason,
    FailureReason,
    ReasonCategory,
    attempt_reason_check,
    category_of,
    evaluation_reason_check,
    is_evaluation_retryable,
    is_retryable,
    outcome_allowed,
    reason_for_model_error,
    run_reason_check,
    sql_values,
)
from agenteval_core.states import COMPLETED_OUTCOMES, FAILED_OUTCOMES, Outcome


def test_policy_table_is_total() -> None:
    assert set(POLICY) == set(FailureReason)


def test_reasons_only_pair_with_failed_outcomes_or_null() -> None:
    for reason, policy in POLICY.items():
        assert policy.outcomes <= FAILED_OUTCOMES, reason
        assert not (policy.outcomes & COMPLETED_OUTCOMES), reason


@pytest.mark.parametrize(
    ("prefix", "outcome"),
    [("model_", Outcome.MODEL_ERROR), ("sandbox_", Outcome.SANDBOX_ERROR)],
)
def test_prefix_families_pair_with_their_outcome(prefix: str, outcome: Outcome) -> None:
    family = [r for r in FailureReason if r.value.startswith(prefix)]
    assert family
    for reason in family:
        assert POLICY[reason].outcomes == frozenset({outcome})


def test_cancellation_reasons_require_a_null_outcome_and_are_not_retryable() -> None:
    for reason in (FailureReason.CANCELLED_BY_USER, FailureReason.CANCELLED_BY_BATCH):
        assert outcome_allowed(reason, None)
        assert not outcome_allowed(reason, Outcome.INTERNAL_ERROR)
        assert not is_retryable(reason)
        assert category_of(reason) is ReasonCategory.CANCELLATION


def test_other_reasons_require_an_outcome() -> None:
    assert not outcome_allowed(FailureReason.LEASE_EXPIRED, None)
    assert outcome_allowed(FailureReason.LEASE_EXPIRED, Outcome.INTERNAL_ERROR)
    assert not outcome_allowed(FailureReason.MODEL_TIMEOUT, Outcome.SANDBOX_ERROR)


def test_unclassified_accepts_any_failed_outcome() -> None:
    for outcome in FAILED_OUTCOMES:
        assert outcome_allowed(FailureReason.UNCLASSIFIED, outcome)
    assert not is_retryable(FailureReason.UNCLASSIFIED)


def test_run_and_attempt_reason_lists_partition_the_enum() -> None:
    assert RUN_LEVEL_REASONS == {FailureReason.INTERNAL_INCONSISTENT, FailureReason.CLAIM_ABANDONED}
    assert RUN_LEVEL_REASONS.isdisjoint(ATTEMPT_REASONS)
    assert RUN_LEVEL_REASONS | ATTEMPT_REASONS == set(FailureReason)
    assert not any(is_retryable(r) for r in RUN_LEVEL_REASONS)


@pytest.mark.parametrize("kind", list(ModelErrorKind))
def test_model_error_kinds_map_onto_reasons_with_the_same_retryability(
    kind: ModelErrorKind,
) -> None:
    reason = reason_for_model_error(kind)
    assert reason.value == f"model_{kind.value}"
    assert is_retryable(reason) == (kind in RETRYABLE_KINDS)


def test_transient_infrastructure_reasons_are_retryable() -> None:
    for reason in (
        FailureReason.SANDBOX_CREATE_FAILED,
        FailureReason.SANDBOX_LOST,
        FailureReason.WORKER_SHUTDOWN,
        FailureReason.LEASE_EXPIRED,
        FailureReason.PERSISTENCE_TRAJECTORY,
    ):
        assert is_retryable(reason), reason
    assert not is_retryable(FailureReason.SANDBOX_IMAGE_NOT_FOUND)


def test_evaluation_retryability() -> None:
    assert EVALUATION_RETRYABLE == {
        EvaluationErrorReason.SANDBOX_FAILED,
        EvaluationErrorReason.BUDGET_TIMEOUT,
        EvaluationErrorReason.INTERNAL_UNEXPECTED,
        EvaluationErrorReason.WORKER_SHUTDOWN,
        EvaluationErrorReason.LEASE_EXPIRED,
    }
    for reason in (
        EvaluationErrorReason.CAPTURE_PERSIST_FAILED,
        EvaluationErrorReason.ARTIFACT_MISSING,
        EvaluationErrorReason.ARTIFACT_CORRUPT,
        EvaluationErrorReason.UNCLASSIFIED,
    ):
        assert not is_evaluation_retryable(reason), reason


def test_sql_lists_are_sorted_and_quoted() -> None:
    assert sql_values(RUN_LEVEL_REASONS) == "'claim_abandoned','internal_inconsistent'"


def test_generated_checks_cover_exactly_their_lists() -> None:
    attempt_sql, run_sql, eval_sql = (
        attempt_reason_check(),
        run_reason_check(),
        evaluation_reason_check(),
    )
    for reason in ATTEMPT_REASONS:
        assert f"'{reason.value}'" in attempt_sql
    for reason in RUN_LEVEL_REASONS:
        assert f"'{reason.value}'" not in attempt_sql
        assert f"'{reason.value}'" in run_sql
    for eval_reason in EvaluationErrorReason:
        assert f"'{eval_reason.value}'" in eval_sql
    assert attempt_sql.startswith("failure_reason IS NULL OR")
