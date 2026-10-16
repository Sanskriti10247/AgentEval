"""Pure parts of the executor (Step 3): the _internal_error -> failure_reason
mapping. No Docker, no database."""

from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome
from agenteval_worker.executor import _internal_error


def test_backstop_timeouts_are_worker_backstop() -> None:
    for message in (
        "sandbox did not return within timeout + grace",
        "agent run did not return within timeout + grace",
    ):
        r = _internal_error(message, FailureReason.WORKER_BACKSTOP)
        assert r.outcome is Outcome.INTERNAL_ERROR
        assert r.failure_reason is FailureReason.WORKER_BACKSTOP
        assert r.error_message == message


def test_agent_tasks_not_enabled_is_worker_capability() -> None:
    r = _internal_error(
        "agent tasks are not enabled on this worker", FailureReason.WORKER_CAPABILITY
    )
    assert (r.outcome, r.failure_reason) == (
        Outcome.INTERNAL_ERROR,
        FailureReason.WORKER_CAPABILITY,
    )


def test_an_unexpected_exception_is_internal_unexpected() -> None:
    r = _internal_error("RuntimeError: boom", FailureReason.INTERNAL_UNEXPECTED)
    assert (r.outcome, r.failure_reason) == (
        Outcome.INTERNAL_ERROR,
        FailureReason.INTERNAL_UNEXPECTED,
    )
