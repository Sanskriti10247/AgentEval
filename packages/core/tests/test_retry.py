import random

import pytest

from agenteval_core.agent.retry import GiveUp, Retry, RetryPolicy, next_model_retry
from agenteval_core.providers.base import RETRYABLE_KINDS, ModelError, ModelErrorKind

POLICY = RetryPolicy(max_retries=3, base_s=2.0, cap_s=30.0, jitter_s=1.0)
NO_JITTER = RetryPolicy(max_retries=5, base_s=2.0, cap_s=10.0, jitter_s=0.0)


def err(kind: ModelErrorKind, retry_after_s: float | None = None) -> ModelError:
    return ModelError(kind, "x", retry_after_s=retry_after_s)


@pytest.mark.parametrize("kind", sorted(RETRYABLE_KINDS))
def test_retryable_kinds_retry(kind: ModelErrorKind) -> None:
    assert isinstance(next_model_retry(err(kind), 0, POLICY, 100, random.Random(0)), Retry)


@pytest.mark.parametrize("kind", sorted(set(ModelErrorKind) - RETRYABLE_KINDS))
def test_non_retryable_kinds_give_up_immediately(kind: ModelErrorKind) -> None:
    decision = next_model_retry(err(kind), 0, POLICY, 100, random.Random(0))
    assert decision == GiveUp("not_retryable")


def test_retryable_classification() -> None:
    assert err(ModelErrorKind.OVERLOADED).retryable
    assert not err(ModelErrorKind.AUTHENTICATION).retryable
    assert not err(ModelErrorKind.BAD_REQUEST).retryable


def test_exponential_backoff_with_cap() -> None:
    delays = [
        next_model_retry(err(ModelErrorKind.SERVER_ERROR), n, NO_JITTER, 1000, random.Random(0))
        for n in range(5)
    ]
    assert delays == [Retry(2.0), Retry(4.0), Retry(8.0), Retry(10.0), Retry(10.0)]


def test_jitter_is_bounded_and_seeded() -> None:
    rng_a, rng_b = random.Random(42), random.Random(42)
    for n in range(3):
        a = next_model_retry(err(ModelErrorKind.OVERLOADED), n, POLICY, 1000, rng_a)
        b = next_model_retry(err(ModelErrorKind.OVERLOADED), n, POLICY, 1000, rng_b)
        assert a == b  # deterministic with a seeded rng
        assert isinstance(a, Retry)
        base = min(2.0 * 2**n, 30.0)
        assert base <= a.delay_s <= base + 1.0


def test_retries_exhausted() -> None:
    decision = next_model_retry(err(ModelErrorKind.OVERLOADED), 3, POLICY, 1000, random.Random(0))
    assert decision == GiveUp("retries_exhausted")


def test_retry_after_overrides_shorter_backoff() -> None:
    decision = next_model_retry(
        err(ModelErrorKind.RATE_LIMITED, 20.0), 0, NO_JITTER, 1000, random.Random(0)
    )
    assert decision == Retry(20.0)


def test_shorter_retry_after_does_not_shrink_backoff() -> None:
    decision = next_model_retry(
        err(ModelErrorKind.RATE_LIMITED, 0.5), 2, NO_JITTER, 1000, random.Random(0)
    )
    assert decision == Retry(8.0)


@pytest.mark.parametrize(("retry_after", "time_left"), [(None, 1.5), (None, 2.0), (60.0, 30.0)])
def test_never_waits_past_the_deadline(retry_after: float | None, time_left: float) -> None:
    decision = next_model_retry(
        err(ModelErrorKind.OVERLOADED, retry_after), 0, NO_JITTER, time_left, random.Random(0)
    )
    assert decision == GiveUp("deadline")
