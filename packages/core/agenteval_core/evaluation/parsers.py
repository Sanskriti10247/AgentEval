"""Parsers for unittest and pytest summaries (C3).

A test check passes only if the process exited 0 AND the parsed summary shows
exactly the expected number of tests, all passing. A missing or unparsable
summary is a failure, never a pass. (A candidate that forges a complete
summary and exits 0 still defeats in-process runners: C7, see ADR-006.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RunCounts:
    total: int  # tests that ran (incl. skipped)
    passed: int
    failed: int  # failures + errors
    skipped: int

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.passed == self.total


_UT_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s$", re.M)
_UT_RESULT = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?$", re.M)
_PT_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:, )?)+) in [\d.]+s(?: \([^)]*\))?\s*=*$",
    re.M,
)


def parse_unittest(output: str) -> RunCounts | None:
    """unittest's TextTestRunner summary (it writes to stderr)."""
    ran = _UT_RAN.findall(output)
    results = _UT_RESULT.findall(output)
    if not ran or not results:
        return None
    total = int(ran[-1])
    verdict, detail = results[-1]
    fields = dict(_kv(detail))
    failed = (
        fields.get("failures", 0) + fields.get("errors", 0) + fields.get("unexpected successes", 0)
    )
    skipped = fields.get("skipped", 0)
    if verdict == "OK" and failed:
        return None  # inconsistent summary: refuse to trust it
    passed = max(total - failed - skipped - fields.get("expected failures", 0), 0)
    return RunCounts(total=total, passed=passed, failed=failed, skipped=skipped)


def parse_pytest(output: str) -> RunCounts | None:
    """pytest's final summary line, e.g. '3 passed, 1 failed in 0.12s'."""
    found = _PT_SUMMARY.findall(output)
    if not found:
        return None
    counts = {name: n for n, name in re.findall(r"(\d+) (\w+)", found[-1])}
    num = {k: int(v) for k, v in counts.items()}
    passed = num.get("passed", 0)
    failed = (
        num.get("failed", 0) + num.get("error", 0) + num.get("errors", 0) + num.get("xpassed", 0)
    )
    skipped = num.get("skipped", 0) + num.get("xfailed", 0)
    return RunCounts(total=passed + failed + skipped, passed=passed, failed=failed, skipped=skipped)


def _kv(detail: str) -> list[tuple[str, int]]:
    pairs = []
    for part in filter(None, (p.strip() for p in detail.split(","))):
        key, _, value = part.partition("=")
        if value.isdigit():
            pairs.append((key.strip(), int(value)))
    return pairs
