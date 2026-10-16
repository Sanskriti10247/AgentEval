"""Retry backoff (F6, F9): min(base * 2^(n-1), cap) * U(0.8, 1.2).

`n` is the number of the attempt that just failed. The jitter source is
injected so tests are deterministic.
"""

import random
from collections.abc import Callable

BASE_S = 2.0
CAP_S = 60.0
JITTER = (0.8, 1.2)


def backoff_delay(
    n: int,
    *,
    base: float = BASE_S,
    cap: float = CAP_S,
    uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    if n < 1:
        raise ValueError("attempt number must be >= 1")
    # Cap the exponent before computing it, so a large n never overflows.
    raw = min(base * 2.0 ** min(n - 1, 62), cap)
    return raw * uniform(*JITTER)
