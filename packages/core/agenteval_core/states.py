"""Run lifecycle and execution outcome.

Two separate concepts (see README "Run status vs execution outcome"):

* RunStatus answers "where is this job in the platform lifecycle?"
* Outcome answers "what happened when the task's command ran?"

COMPLETED never means the task succeeded; it means the platform produced a
trustworthy outcome. FAILED means the platform could not.
"""

from enum import StrEnum

from agenteval_core.reliability.transitions import RUN_TRANSITIONS, RunState


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Outcome(StrEnum):
    # Command tasks (S1)
    SUCCESS = "SUCCESS"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    # Both task types: wall-clock limit hit
    TIMEOUT = "TIMEOUT"
    # Agent tasks (Phase 3): how the agent's run ended. Whether it *solved*
    # the task is Phase 4's evaluator, not the outcome.
    AGENT_FINISHED = "AGENT_FINISHED"
    MAX_STEPS = "MAX_STEPS"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MODEL_REFUSAL = "MODEL_REFUSAL"
    # Platform faults: the task was never fairly tested
    MODEL_ERROR = "MODEL_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)

# The only legal transitions, derived from the reliability state machine
# (reliability.transitions.RUN_TRANSITIONS) so the two can never drift apart:
# a unit test asserts this equality directly. Repositories also guard every
# UPDATE with `WHERE status = <from>` so the database enforces this under
# concurrency.
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus(src.value): frozenset(RunStatus(t.dst.value) for t in RUN_TRANSITIONS if t.src == src)
    for src in RunState
}

# Outcomes that are trustworthy results about the task/agent -> COMPLETED.
# Everything else is a platform fault -> FAILED (retryable in Phase 5).
COMPLETED_OUTCOMES: frozenset[Outcome] = frozenset(
    {
        Outcome.SUCCESS,
        Outcome.NON_ZERO_EXIT,
        Outcome.TIMEOUT,
        Outcome.AGENT_FINISHED,
        Outcome.MAX_STEPS,
        Outcome.BUDGET_EXCEEDED,
        Outcome.MODEL_REFUSAL,
    }
)
FAILED_OUTCOMES: frozenset[Outcome] = frozenset(Outcome) - COMPLETED_OUTCOMES


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

    Task- and agent-caused outcomes (including TIMEOUT, NON_ZERO_EXIT,
    MAX_STEPS and MODEL_REFUSAL) are definitive results -> COMPLETED.
    Platform faults (MODEL_ERROR, SANDBOX_ERROR, INTERNAL_ERROR) -> FAILED.
    """
    return RunStatus.COMPLETED if outcome in COMPLETED_OUTCOMES else RunStatus.FAILED


def sql_in_list(values: frozenset[Outcome] | type[Outcome]) -> str:
    """Render enum values as a sorted SQL IN-list body, e.g. "'A','B'"."""
    return ",".join(f"'{v.value}'" for v in sorted(values, key=lambda o: o.value))
