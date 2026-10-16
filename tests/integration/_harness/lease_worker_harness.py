"""Standalone harness process for test_leases.py's worker-killed scenario.

Started as a real OS process (not a pytest-managed subprocess, and not an
asyncio task) so the test can SIGKILL it from outside and observe that the
real lease keeper subprocess it spawned exits on its own shortly after
(parent-death detection, ADR-008 D) - not merely that an in-process cancel
fired.

Run with the same interpreter as the test (so the project's venv is used):
    python lease_worker_harness.py <database_url> <ttl_s> <margin_s> <pid_file>

Writes the keeper subprocess's pid to <pid_file> once it exists, then blocks
until killed.
"""

import asyncio
import sys
import time

from agenteval_core.reliability.fencing import LeaseTiming
from agenteval_worker.lease_manager import LeaseManager


async def main() -> None:
    database_url, ttl_s, margin_s, pid_file = sys.argv[1:5]
    timing = LeaseTiming(ttl_s=float(ttl_s), heartbeat_s=0.3, margin_s=float(margin_s))
    lm = LeaseManager(database_url, timing, "w-harness")
    await lm.start()
    lm.hold(
        "run",
        "00000000-0000-0000-0000-000000000000",
        1,
        time.monotonic(),
        on_lease_lost=lambda: None,
    )
    assert lm._process is not None
    with open(pid_file, "w") as f:  # noqa: ASYNC230 - a one-off startup write, not a hot path
        f.write(str(lm._process.pid))
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
