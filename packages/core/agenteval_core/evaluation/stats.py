"""Success-rate semantics (D7).

graded attempts      evaluations that SUCCEEDED (the agent was fairly tested)
success rate         passed / graded, with a Wilson 95% interval
platform failures    attempts the platform broke (platform_*): excluded from
                     the denominator and reported separately, so a dead
                     model API can't make an agent look bad
evaluation errors    the evaluator broke (ERROR): also excluded and reported
ungraded             SKIPPED without a platform failure (no evaluation spec)
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from agenteval_core.evaluation.taxonomy import (
    PLATFORM_CATEGORIES,
    EvaluationStatus,
    FailureCategory,
)

Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n == 0."""
    if n == 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n")
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # The interval's endpoints are exactly 0 / 1 at k = 0 / k = n; computing
    # them in floating point can land a hair inside (found by a property test).
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high


@dataclass(frozen=True)
class SuccessStats:
    attempts: int
    graded: int
    passed: int
    success_rate: float | None
    ci95: tuple[float, float] | None
    platform_failures: int
    platform_failure_rate: float | None  # over all attempts
    evaluation_errors: int
    ungraded: int


def success_stats(
    results: Iterable[tuple[EvaluationStatus, FailureCategory | None]],
) -> SuccessStats:
    attempts = graded = passed = platform = errors = ungraded = 0
    for status, category in results:
        attempts += 1
        if category in PLATFORM_CATEGORIES:
            platform += 1
        elif status is EvaluationStatus.ERROR:
            errors += 1
        elif status is EvaluationStatus.SUCCEEDED:
            graded += 1
            passed += category is FailureCategory.PASSED
        else:  # SKIPPED (no spec), or still PENDING/RUNNING
            ungraded += 1
    return SuccessStats(
        attempts=attempts,
        graded=graded,
        passed=passed,
        success_rate=passed / graded if graded else None,
        ci95=wilson_interval(passed, graded),
        platform_failures=platform,
        platform_failure_rate=platform / attempts if attempts else None,
        evaluation_errors=errors,
        ungraded=ungraded,
    )
