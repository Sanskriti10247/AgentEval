"""EvaluationSpec validation: check kinds, C6 argv rules, reserved /eval names."""

from typing import Any

import pytest
from pydantic import ValidationError

from agenteval_core.evaluation.spec import EvaluationSpec, validate_against_task

TESTS = {"test_calc.py": "import unittest\n"}


def spec(**kw: Any) -> EvaluationSpec:
    base: dict[str, Any] = {
        "checks": [{"kind": "python_tests", "name": "hidden", "expected_tests": 2}],
        "hidden_files": TESTS,
    }
    return EvaluationSpec.model_validate({**base, **kw})


def test_minimal_spec_and_defaults() -> None:
    s = spec()
    check = s.checks[0]
    assert (check.kind, check.framework, check.pattern, check.required, check.weight) == (
        "python_tests",
        "unittest",
        "test*.py",
        True,
        1.0,
    )
    assert s.timeout_s == 120 and s.protected_paths == []


def test_every_kind_validates() -> None:
    s = spec(
        checks=[
            {"kind": "python_tests", "name": "t", "framework": "pytest", "expected_tests": 3},
            {
                "kind": "io",
                "name": "io",
                "argv": ["python", "-I", "main.py"],
                "stdin": "2 3",
                "expected_stdout": "5",
            },
            {
                "kind": "command",
                "name": "c",
                "argv": ["sh", "check.sh"],
                "trusted_environment": True,
                "timeout_s": 10,
            },
            {
                "kind": "final_answer",
                "name": "a",
                "match": "number",
                "expected": "42",
                "tolerance": 0.5,
            },
            {
                "kind": "file",
                "name": "f",
                "path": "out/result.json",
                "mode": "json_equals",
                "expected": '{"ok": true}',
            },
            {
                "kind": "final_answer",
                "name": "j",
                "match": "json_equals",
                "expected": "null",
                "required": False,
            },
        ],
        timeout_s=300,
    )
    assert [c.kind for c in s.checks] == [
        "python_tests",
        "io",
        "command",
        "final_answer",
        "file",
        "final_answer",
    ]


def test_json_null_is_a_valid_expected_value() -> None:
    """Regression: an early draft treated JSON null as invalid JSON."""
    spec(checks=[{"kind": "final_answer", "name": "n", "match": "json_equals", "expected": "null"}])


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"checks": []}, "at least 1"),
        (
            {
                "checks": [
                    {"kind": "python_tests", "name": "x", "expected_tests": 1, "required": False}
                ]
            },
            "at least one check must be required",
        ),
        (
            {
                "checks": [
                    {"kind": "final_answer", "name": "a", "expected": "1"},
                    {"kind": "final_answer", "name": "a", "expected": "2"},
                ]
            },
            "unique",
        ),
        (
            {
                "checks": [
                    {
                        "kind": "python_tests",
                        "name": "x",
                        "expected_tests": 1,
                        "pattern": "check_*.py",
                    }
                ]
            },
            "no hidden file matches",
        ),
        (
            {"checks": [{"kind": "python_tests", "name": "x", "expected_tests": 0}]},
            "expected_tests",
        ),
        # C6: argv[0] must be a bare interpreter name, never a (workspace) path
        (
            {
                "checks": [
                    {"kind": "io", "name": "x", "argv": ["./solution"], "expected_stdout": ""}
                ]
            },
            "bare program name",
        ),
        (
            {
                "checks": [
                    {
                        "kind": "io",
                        "name": "x",
                        "argv": ["/workspace/run.sh"],
                        "expected_stdout": "",
                    }
                ]
            },
            "bare program name",
        ),
        ({"checks": [{"kind": "io", "name": "x", "argv": [], "expected_stdout": ""}]}, "empty"),
        # C6/C7: free-form commands must acknowledge they are trusted-environment only
        (
            {"checks": [{"kind": "command", "name": "x", "argv": ["sh", "t.sh"]}]},
            "trusted_environment",
        ),
        (
            {
                "checks": [
                    {"kind": "command", "name": "x", "argv": ["sh"], "trusted_environment": False}
                ]
            },
            "trusted_environment",
        ),
        (
            {
                "checks": [
                    {"kind": "final_answer", "name": "x", "match": "regex", "expected": "(unclosed"}
                ]
            },
            "invalid regex",
        ),
        (
            {
                "checks": [
                    {"kind": "final_answer", "name": "x", "match": "number", "expected": "forty"}
                ]
            },
            "numeric",
        ),
        ({"checks": [{"kind": "file", "name": "x", "path": "../etc/passwd"}]}, "path"),
        (
            {"checks": [{"kind": "file", "name": "x", "path": "a.txt", "mode": "equals"}]},
            "needs an expected value",
        ),
        (
            {
                "checks": [
                    {
                        "kind": "file",
                        "name": "x",
                        "path": "a.txt",
                        "mode": "sha256",
                        "expected": "ABC",
                    }
                ]
            },
            "hex digest",
        ),
        ({"hidden_files": {"conftest.py": "x", **TESTS}}, "reserved"),
        ({"hidden_files": {".agenteval/run.py": "x", **TESTS}}, "reserved"),
        ({"hidden_files": {"../escape.py": "x", **TESTS}}, "path"),
        ({"protected_paths": ["calc.py", "calc.py"]}, "unique"),
        (
            {
                "timeout_s": 30,
                "checks": [
                    {"kind": "python_tests", "name": "x", "expected_tests": 1, "timeout_s": 60}
                ],
            },
            "exceed the evaluation timeout",
        ),
        ({"surprise": True}, "Extra inputs"),
    ],
)
def test_invalid_specs(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        spec(**overrides)


def test_hidden_files_total_size_cap() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        spec(hidden_files={**TESTS, "big.txt": "x" * (1024 * 1024)})


def test_protected_paths_must_be_task_files() -> None:
    s = spec(protected_paths=["test_visible.py"])
    validate_against_task(s, {"test_visible.py": "x"})
    with pytest.raises(ValueError, match="not task files"):
        validate_against_task(s, {"calc.py": "x"})


def test_spec_hash_is_stable_and_sensitive() -> None:
    a = spec()
    b = EvaluationSpec.model_validate_json(a.model_dump_json())
    assert a.spec_hash() == b.spec_hash() and len(a.spec_hash()) == 64
    assert spec(hidden_files={"test_calc.py": "changed"}).spec_hash() != a.spec_hash()
