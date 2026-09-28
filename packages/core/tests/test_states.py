import pytest

from agenteval_core.states import (
    ALLOWED_TRANSITIONS,
    COMPLETED_OUTCOMES,
    FAILED_OUTCOMES,
    TERMINAL_STATUSES,
    IllegalTransition,
    Outcome,
    RunStatus,
    can_transition,
    check_transition,
    sql_in_list,
    status_for,
)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.SUCCESS, RunStatus.COMPLETED),
        (Outcome.NON_ZERO_EXIT, RunStatus.COMPLETED),
        (Outcome.TIMEOUT, RunStatus.COMPLETED),
        (Outcome.AGENT_FINISHED, RunStatus.COMPLETED),
        (Outcome.MAX_STEPS, RunStatus.COMPLETED),
        (Outcome.BUDGET_EXCEEDED, RunStatus.COMPLETED),
        (Outcome.MODEL_REFUSAL, RunStatus.COMPLETED),  # model behaviour, attributable to the agent
        (Outcome.MODEL_ERROR, RunStatus.FAILED),  # provider fault: never fairly tested
        (Outcome.SANDBOX_ERROR, RunStatus.FAILED),
        (Outcome.INTERNAL_ERROR, RunStatus.FAILED),
    ],
)
def test_status_for(outcome: Outcome, expected: RunStatus) -> None:
    assert status_for(outcome) is expected


def test_every_outcome_maps_to_a_terminal_status() -> None:
    for outcome in Outcome:
        assert status_for(outcome) in TERMINAL_STATUSES


def test_happy_path_is_allowed() -> None:
    path = [RunStatus.QUEUED, RunStatus.CLAIMED, RunStatus.RUNNING, RunStatus.COMPLETED]
    for src, dst in zip(path, path[1:], strict=False):
        check_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),  # must be claimed first
        (RunStatus.QUEUED, RunStatus.COMPLETED),
        (RunStatus.CLAIMED, RunStatus.QUEUED),
        (RunStatus.RUNNING, RunStatus.QUEUED),  # no retries in the slice
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.QUEUED),
    ],
)
def test_illegal_transitions_rejected(src: RunStatus, dst: RunStatus) -> None:
    assert not can_transition(src, dst)
    with pytest.raises(IllegalTransition):
        check_transition(src, dst)


def test_terminal_statuses_have_no_exits() -> None:
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(RunStatus)


def test_completed_and_failed_outcomes_partition_the_enum() -> None:
    assert COMPLETED_OUTCOMES | FAILED_OUTCOMES == frozenset(Outcome)
    assert COMPLETED_OUTCOMES & FAILED_OUTCOMES == frozenset()
    assert FAILED_OUTCOMES == {Outcome.MODEL_ERROR, Outcome.SANDBOX_ERROR, Outcome.INTERNAL_ERROR}


def test_sql_in_list_is_sorted_and_quoted() -> None:
    assert sql_in_list(FAILED_OUTCOMES) == "'INTERNAL_ERROR','MODEL_ERROR','SANDBOX_ERROR'"
