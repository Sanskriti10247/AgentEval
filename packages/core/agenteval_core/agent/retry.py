"""In-step retry policy for model calls (a pure function, easy to test).

SDK-level retries are disabled so that every retry is visible: the runtime
records each failed try as a model_error trajectory event before waiting.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from agenteval_core.providers.base import ModelError


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_s: float = 2.0
    cap_s: float = 30.0
    jitter_s: float = 1.0


@dataclass(frozen=True)
class Retry:
    delay_s: float


@dataclass(frozen=True)
class GiveUp:
    reason: str  # "not_retryable" | "retries_exhausted" | "deadline"


def next_model_retry(
    error: ModelError,
    retries_done: int,
    policy: RetryPolicy,
    time_left_s: float,
    rng: random.Random,
) -> Retry | GiveUp:
    """Decide what to do after a failed model call.

    delay = max(min(base * 2^retries_done, cap) + U(0, jitter), retry-after)

    The server's retry-after wins over our own backoff, but never over the
    run's deadline: if waiting would leave no time for the call, give up.
    """
    if not error.retryable:
        return GiveUp("not_retryable")
    if retries_done >= policy.max_retries:
        return GiveUp("retries_exhausted")
    backoff = min(policy.base_s * (2**retries_done), policy.cap_s)
    delay = backoff + rng.uniform(0, policy.jitter_s)
    if error.retry_after_s is not None:
        delay = max(delay, error.retry_after_s)
    if delay >= time_left_s:
        return GiveUp("deadline")
    return Retry(delay_s=delay)
