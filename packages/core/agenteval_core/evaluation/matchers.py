"""Text, number, JSON and regex matching for final answers, io stdout and
files. Pure; the candidate side is always untrusted and length-capped."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

# Untrusted text is capped before matching (bounds regex work, see ADR-006).
MAX_ACTUAL_CHARS = 64 * 1024
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

# (summary when it matches, summary when it doesn't). Internal summaries that
# never contain the expected value itself (D11).
_REASONS = {
    "exact": ("exact match", "differs from the expected value"),
    "normalized": (
        "matches (ignoring case and whitespace)",
        "differs from the expected value (ignoring case and whitespace)",
    ),
    "contains": ("contains the expected text", "does not contain the expected text"),
    "regex": ("matches the expected pattern", "does not match the expected pattern"),
    "number": (
        "within tolerance of the expected number",
        "outside the tolerance of the expected number",
    ),
    "json_equals": ("JSON-equal to the expected value", "JSON differs from the expected value"),
}


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    reason: str


def cap(text: str) -> str:
    return text[:MAX_ACTUAL_CHARS]


def normalize(text: str) -> str:
    """Case-insensitive, whitespace-insensitive comparison form."""
    return " ".join(text.split()).casefold()


def parse_number(text: str) -> float | None:
    stripped = text.strip()
    if not _NUMBER.fullmatch(stripped):
        return None
    value = float(stripped)
    return value if math.isfinite(value) else None


def load_json(text: str) -> tuple[bool, Any]:
    """(valid, value). Explicit validity, because JSON `null` is valid and None."""
    try:
        return True, json.loads(text)
    except (ValueError, RecursionError):
        return False, None


def match_text(mode: str, expected: str, actual: str, *, tolerance: float = 0.0) -> MatchResult:
    if mode not in _REASONS:
        raise ValueError(f"unknown match mode {mode!r}")
    actual = cap(actual)
    if mode == "exact":
        ok = actual == expected
    elif mode == "normalized":
        ok = normalize(actual) == normalize(expected)
    elif mode == "contains":
        ok = expected in actual
    elif mode == "regex":
        ok = re.fullmatch(expected, actual.strip(), flags=re.DOTALL) is not None
    elif mode == "number":
        got, want = parse_number(actual), parse_number(expected)
        if got is None or want is None:
            return MatchResult(False, "is not a number")
        ok = abs(got - want) <= tolerance
    else:  # json_equals
        valid, got_json = load_json(actual)
        if not valid:
            return MatchResult(False, "is not valid JSON")
        ok = got_json == load_json(expected)[1]
    yes, no = _REASONS[mode]
    return MatchResult(ok, yes if ok else no)
