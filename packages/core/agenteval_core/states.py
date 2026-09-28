"""Run lifecycle and execution outcome.

Two separate concepts (see README "Run status vs execution outcome"):

* RunStatus answers "where is this job in the platform lifecycle?"
* Outcome answers "what happened when the task's command ran?"

COMPLETED never means the task succeeded; it means the platform produced a
trustworthy outcome. FAILED means the platform could not.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    TIMEOUT = "TIMEOUT"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})

# The only legal transitions. Repositories also guard every UPDATE with
# `WHERE status = <from>` so the database enforces this under concurrency.
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.CLAIMED}),
    RunStatus.CLAIMED: frozenset({RunStatus.RUNNING, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
}

_TASK_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.NON_ZERO_EXIT, Outcome.TIMEOUT})


class IllegalTransition(Exception):
    def __init__(self, src: RunStatus, dst: RunStatus) -> None:
        super().__init__(f"illegal run transition {src} -> {dst}")
        self.src, self.dst = src, dst


def can_transition(src: RunStatus, dst: RunStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


def check_transition(src: RunStatus, dst: RunStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def status_for(outcome: Outcome) -> RunStatus:
    """Map an execution outcome to the terminal run status it produces.

    Task-caused outcomes (including TIMEOUT and NON_ZERO_EXIT) are definitive
    results -> COMPLETED. Platform faults -> FAILED (retryable in Phase 5).
    """
    return RunStatus.COMPLETED if outcome in _TASK_OUTCOMES else RunStatus.FAILED
