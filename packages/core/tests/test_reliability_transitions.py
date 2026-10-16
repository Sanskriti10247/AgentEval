import pytest

from agenteval_core.evaluation.taxonomy import EvaluationStatus
from agenteval_core.reliability.transitions import (
    ATTEMPT_TERMINAL,
    ATTEMPT_TRANSITIONS,
    EVALUATION_TERMINAL,
    EVALUATION_TRANSITIONS,
    RUN_LEASED,
    RUN_REOPENABLE,
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    Actor,
    AttemptState,
    EvaluationState,
    RunState,
    allowed,
    is_allowed,
)
from agenteval_core.states import AttemptStatus, RunStatus


def test_codes_are_unique() -> None:
    for table in (RUN_TRANSITIONS, ATTEMPT_TRANSITIONS, EVALUATION_TRANSITIONS):
        codes = [t.code for t in table]
        assert len(codes) == len(set(codes))


def test_phase5_states_extend_the_existing_ones() -> None:
    assert {s.value for s in RunStatus} <= {s.value for s in RunState}
    assert {s.value for s in AttemptStatus} <= {s.value for s in AttemptState}
    assert {s.value for s in EvaluationStatus} == {s.value for s in EvaluationState}


def test_terminal_run_states_have_no_exit_except_replay() -> None:
    for t in RUN_TRANSITIONS:
        if t.src in RUN_TERMINAL:
            assert t.src in RUN_REOPENABLE and t.code == "R14" and t.dst is RunState.QUEUED
    assert RUN_REOPENABLE == {RunState.FAILED}


@pytest.mark.parametrize(
    ("table", "terminal"),
    [(ATTEMPT_TRANSITIONS, ATTEMPT_TERMINAL), (EVALUATION_TRANSITIONS, EVALUATION_TERMINAL)],
)
def test_attempt_and_evaluation_terminal_states_have_no_exit(table, terminal) -> None:  # type: ignore[no-untyped-def]
    assert not [t for t in table if t.src in terminal]


def test_every_state_is_reachable() -> None:
    for table, states in (
        (RUN_TRANSITIONS, RunState),
        (ATTEMPT_TRANSITIONS, AttemptState),
        (EVALUATION_TRANSITIONS, EvaluationState),
    ):
        reached = {t.dst for t in table}
        initial = {RunState.QUEUED}
        assert set(states) <= reached | initial


@pytest.mark.parametrize(
    ("src", "dst", "codes"),
    [
        (RunState.QUEUED, RunState.CLAIMED, ["R1"]),
        (RunState.CLAIMED, RunState.QUEUED, ["R4a"]),
        (RunState.CLAIMED, RunState.FAILED, ["R4b", "R6"]),
        (RunState.RUNNING, RunState.RETRYING, ["R8", "R9"]),
        (RunState.RETRYING, RunState.QUEUED, ["R12"]),
        (RunState.FAILED, RunState.QUEUED, ["R14"]),
    ],
)
def test_allowed_run_moves(src: RunState, dst: RunState, codes: list[str]) -> None:
    assert [t.code for t in allowed(RUN_TRANSITIONS, src, dst)] == codes


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (RunState.QUEUED, RunState.RUNNING),
        (RunState.RUNNING, RunState.QUEUED),
        (RunState.COMPLETED, RunState.QUEUED),
        (RunState.CANCELLED, RunState.QUEUED),
        (RunState.RETRYING, RunState.RUNNING),
        (RunState.CLAIMED, RunState.COMPLETED),
    ],
)
def test_forbidden_run_moves(src: RunState, dst: RunState) -> None:
    assert not is_allowed(RUN_TRANSITIONS, src, dst)


def test_every_non_terminal_run_state_can_be_cancelled() -> None:
    for src in set(RunState) - RUN_TERMINAL:
        assert is_allowed(RUN_TRANSITIONS, src, RunState.CANCELLED), src


def test_only_the_reconciler_recovers_and_only_the_api_replays() -> None:
    by_code = {t.code: t for t in RUN_TRANSITIONS}
    for code in ("R4a", "R4b", "R9", "R12"):
        assert by_code[code].actors == {Actor.RECONCILER}
    assert by_code["R14"].actors == {Actor.API}
    assert RUN_LEASED == {RunState.CLAIMED, RunState.RUNNING}


def test_attempts_and_evaluations_are_created_once() -> None:
    assert [t.code for t in ATTEMPT_TRANSITIONS if t.src is None] == ["A1"]
    assert {t.code for t in EVALUATION_TRANSITIONS if t.src is None} == {"E1", "E2", "E3"}


def test_pending_is_reached_only_through_budget_guarded_transitions() -> None:
    into_pending = {t.code for t in EVALUATION_TRANSITIONS if t.dst is EvaluationState.PENDING}
    assert into_pending == {"E1", "E7", "E8a", "E9a"}
