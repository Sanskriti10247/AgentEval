"""Matchers, and the unittest/pytest parsers against REAL tool output
(produced by running the actual frameworks in a subprocess)."""

import subprocess
import sys
from pathlib import Path

import pytest

from agenteval_core.evaluation.matchers import (
    MAX_ACTUAL_CHARS,
    load_json,
    match_text,
    normalize,
    parse_number,
)
from agenteval_core.evaluation.parsers import RunCounts, parse_pytest, parse_unittest

# -- matchers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected", "actual", "ok"),
    [
        ("exact", "42", "42", True),
        ("exact", "42", "42\n", False),
        ("normalized", "Hello  World", "  hello\nworld ", True),
        ("normalized", "hello", "hello!", False),
        ("contains", "needle", "hay needle stack", True),
        ("contains", "needle", "haystack", False),
        ("regex", r"\d{3}", " 123 ", True),  # fullmatch on the stripped answer
        ("regex", r"\d{3}", "1234", False),
        ("number", "3.14", "3.1400", True),
        ("number", "3.14", "pi", False),
        ("json_equals", '{"a": 1, "b": [1, 2]}', '{"b":[1,2],"a":1}', True),
        ("json_equals", '{"a": 1}', '{"a": 2}', False),
        ("json_equals", "null", "null", True),
        ("json_equals", "null", "not json", False),
    ],
)
def test_match_text(mode: str, expected: str, actual: str, ok: bool) -> None:
    assert match_text(mode, expected, actual).ok is ok


def test_number_tolerance() -> None:
    assert match_text("number", "10", "10.4", tolerance=0.5).ok
    assert not match_text("number", "10", "10.6", tolerance=0.5).ok


def test_reasons_never_contain_the_expected_value() -> None:
    secret = "SECRET-7f3a"
    for mode in ("exact", "normalized", "contains", "regex"):
        for actual in (secret, "nope"):
            assert secret not in match_text(mode, secret, actual).reason


def test_untrusted_text_is_capped_before_matching() -> None:
    huge = "a" * (MAX_ACTUAL_CHARS * 2) + "TAIL"
    assert not match_text("contains", "TAIL", huge).ok


def test_helpers() -> None:
    assert normalize("  A\tb  C ") == "a b c"
    assert parse_number(" -1.5e3 ") == -1500.0
    assert parse_number("1,000") is None and parse_number("inf") is None
    assert load_json("null") == (True, None) and load_json("{") == (False, None)
    with pytest.raises(ValueError):
        match_text("fuzzy", "a", "a")


# -- parsers on real framework output ---------------------------------------------------------

TESTS = """
import unittest

class T(unittest.TestCase):
    def test_ok_1(self): self.assertTrue(True)
    def test_ok_2(self): self.assertTrue(True)
    def test_fail(self): self.assertEqual(1, 2)
    def test_error(self): raise RuntimeError("boom")
    @unittest.skip("later")
    def test_skip(self): pass
"""
PASSING = """
import unittest

class T(unittest.TestCase):
    def test_a(self): self.assertTrue(True)
    def test_b(self): self.assertTrue(True)
"""


def run(tmp_path: Path, source: str, *argv: str) -> str:
    (tmp_path / "test_sample.py").write_text(source)
    proc = subprocess.run(
        [sys.executable, *argv], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    return proc.stdout + "\n" + proc.stderr


def test_parse_real_unittest_failure(tmp_path: Path) -> None:
    out = run(tmp_path, TESTS, "-m", "unittest", "-q")
    assert parse_unittest(out) == RunCounts(total=5, passed=2, failed=2, skipped=1)


def test_parse_real_unittest_success(tmp_path: Path) -> None:
    counts = parse_unittest(run(tmp_path, PASSING, "-m", "unittest"))
    assert counts == RunCounts(total=2, passed=2, failed=0, skipped=0) and counts.all_passed


def test_parse_real_pytest_failure(tmp_path: Path) -> None:
    out = run(tmp_path, TESTS, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    # pytest reports the raised RuntimeError as a failure too: 2 failed.
    assert parse_pytest(out) == RunCounts(total=5, passed=2, failed=2, skipped=1)


def test_parse_real_pytest_success(tmp_path: Path) -> None:
    counts = parse_pytest(run(tmp_path, PASSING, "-m", "pytest", "-q", "-p", "no:cacheprovider"))
    assert counts == RunCounts(total=2, passed=2, failed=0, skipped=0)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Traceback (most recent call last):\n  ...\nImportError: no module named calc",
        "Ran 2 tests in 0.001s\n",  # no verdict line
        "OK\n",  # no run line
        "Ran 2 tests in 0.001s\n\nOK (failures=1)\n",  # inconsistent: refuse to trust
    ],
)
def test_unparsable_unittest_output_is_none(output: str) -> None:
    assert parse_unittest(output) is None


def test_unparsable_pytest_output_is_none() -> None:
    assert parse_pytest("collected 0 items\nERROR: file not found") is None


def test_last_summary_wins() -> None:
    """A forged summary printed early is overridden by the real one at the end."""
    out = "Ran 9 tests in 0.0s\n\nOK\n...\nRan 2 tests in 0.1s\n\nFAILED (failures=1)\n"
    assert parse_unittest(out) == RunCounts(total=2, passed=1, failed=1, skipped=0)
