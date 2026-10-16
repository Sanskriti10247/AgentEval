"""Phase 5 state machines as data: runs (R1-R14), attempts (A1-A4) and
attempt evaluations (E1-E9).

Each transition is applied by one guarded UPDATE (or INSERT for creations);
these tables are the reference the repositories and the tests check against.
Migration 0004 widens the status CHECKs to these states.
"""

from dataclasses import dataclass
from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"


class EvaluationState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class Actor(StrEnum):
    API = "api"
    WORKER = "worker"  # the lease owner
    RECONCILER = "reconciler"


@dataclass(frozen=True)
class Transition[S: StrEnum]:
    code: str
    src: S | None  # None: the row is created by this transition
    dst: S
    actors: frozenset[Actor]


def _t[S: StrEnum](code: str, src: S | None, dst: S, *actors: Actor) -> Transition[S]:
    return Transition(code, src, dst, frozenset(actors))


_Q, _C, _R = RunState.QUEUED, RunState.CLAIMED, RunState.RUNNING
_RT, _OK, _F, _X = RunState.RETRYING, RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED
_API, _W, _REC = Actor.API, Actor.WORKER, Actor.RECONCILER

RUN_TRANSITIONS: tuple[Transition[RunState], ...] = (
    _t("R1", _Q, _C, _W),  # claim: epoch + 1, lease
    _t("R2", _Q, _X, _API),  # cancel while queued
    _t("R3", _C, _R, _W),  # start: attempt_count + 1, attempt created (A1)
    _t("R4a", _C, _Q, _REC),  # claim lease expired before start, under the limit
    _t("R4b", _C, _F, _REC),  # ... at the limit: claim_abandoned
    _t("R5", _C, _X, _W, _REC),  # cancel while claimed
    _t("R6", _C, _F, _W),  # run or task inconsistent at claim time
    _t("R7", _R, _OK, _W),  # finish with a completed outcome
    _t("R8", _R, _RT, _W),  # retryable failure, budget left
    _t("R9", _R, _RT, _REC),  # lease expired, budget left (attempt A3)
    _t("R10", _R, _F, _W, _REC),  # non-retryable or budget exhausted
    _t("R11", _R, _X, _W, _REC),  # cancel while running
    _t("R12", _RT, _Q, _REC),  # retry due
    _t("R13", _RT, _X, _API),  # cancel while waiting to retry
    _t("R14", _F, _Q, _API),  # replay
)

_AR, _AF, _AA, _AX = (
    AttemptState.RUNNING,
    AttemptState.FINISHED,
    AttemptState.ABANDONED,
    AttemptState.CANCELLED,
)

ATTEMPT_TRANSITIONS: tuple[Transition[AttemptState], ...] = (
    _t("A1", None, _AR, _W),  # inside R3
    _t("A2", _AR, _AF, _W),  # inside R7, R8, R10
    _t("A3", _AR, _AA, _REC),  # inside R9, R10, R11 by the reconciler
    _t("A4", _AR, _AX, _W),  # inside R11 by the owner
)

_EP, _ER, _ES = EvaluationState.PENDING, EvaluationState.RUNNING, EvaluationState.SUCCEEDED
_EE, _EK = EvaluationState.ERROR, EvaluationState.SKIPPED

EVALUATION_TRANSITIONS: tuple[Transition[EvaluationState], ...] = (
    _t("E1", None, _EP, _W, _REC),  # fast path or A1 recovery
    _t("E2", None, _EK, _W, _REC),
    _t("E3", None, _EE, _W, _REC),  # non-retryable at creation
    _t("E4", _EP, _ER, _W),  # claim: epoch + 1, eval_attempts + 1
    _t("E5", _ER, _ES, _W),
    _t("E6", _ER, _EE, _W),
    _t("E7", _ER, _EP, _W),  # transient failure, budget left
    _t("E8a", _ER, _EP, _W),  # shutdown release, budget left
    _t("E8b", _ER, _EE, _W),  # shutdown release, budget exhausted
    _t("E9a", _ER, _EP, _REC),  # lease expired, budget left
    _t("E9b", _ER, _EE, _REC),  # lease expired, budget exhausted
)

RUN_TERMINAL: frozenset[RunState] = frozenset({_OK, _F, _X})
ATTEMPT_TERMINAL: frozenset[AttemptState] = frozenset({_AF, _AA, _AX})
EVALUATION_TERMINAL: frozenset[EvaluationState] = frozenset({_ES, _EE, _EK})
# Only replay (R14) leaves a terminal run state.
RUN_REOPENABLE: frozenset[RunState] = frozenset({_F})
# A cancel request (cancel_requested_at) can only exist on these run states (G11).
RUN_CANCEL_FLAG_STATES: frozenset[RunState] = frozenset({_C, _R, _X})
# Run states that hold a lease (CHECK ck_runs_lease).
RUN_LEASED: frozenset[RunState] = frozenset({_C, _R})


def allowed[S: StrEnum](
    table: tuple[Transition[S], ...], src: S | None, dst: S
) -> list[Transition[S]]:
    """The transitions that move `src` to `dst` (empty if the move is illegal)."""
    return [t for t in table if t.src == src and t.dst == dst]


def is_allowed[S: StrEnum](table: tuple[Transition[S], ...], src: S | None, dst: S) -> bool:
    return bool(allowed(table, src, dst))
