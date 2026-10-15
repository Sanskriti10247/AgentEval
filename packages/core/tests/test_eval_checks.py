"""Plan and judge each check kind (pure; no Docker)."""

from typing import Any

import pytest

from agenteval_core.evaluation.checks import (
    EXEC_ENV,
    NOPYC,
    evaluator_files,
    evidence_for,
    execution_order,
    file_paths_needed,
    judge,
    judge_static,
    plan,
)
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.taxonomy import FailureCategory
from agenteval_core.tools.workspace import ExecOutput

RULE = "-" * 70
UT_OK = f"..\n{RULE}\nRan 2 tests in 0.001s\n\nOK\n"
UT_FAIL = f"F.\n{RULE}\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n"


def build(*checks: dict[str, Any], **kw: Any) -> EvaluationSpec:
    return EvaluationSpec.model_validate(
        {
            "checks": list(checks),
            "hidden_files": {"test_hidden.py": "x", "test_more.py": "y"},
            "timeout_s": 600,
            **kw,
        }
    )


PT = {"kind": "python_tests", "name": "hidden", "expected_tests": 2}
PY = {"kind": "python_tests", "name": "hidden_pytest", "framework": "pytest", "expected_tests": 3}
IO = {
    "kind": "io",
    "name": "io",
    "argv": ["python", "solve.py"],
    "stdin": "SECRET-IN",
    "expected_stdout": "5",
}
CMD = {"kind": "command", "name": "cmd", "argv": ["sh", "check.sh"], "trusted_environment": True}
ANS = {"kind": "final_answer", "name": "answer", "match": "number", "expected": "42"}
FILE = {"kind": "file", "name": "out", "path": "out.txt", "mode": "equals", "expected": "hello\n"}


# -- planning ----------------------------------------------------------------------------------


def test_python_tests_use_the_hardened_evaluator_invocation() -> None:
    s = build(PT, PY)
    unittest_plan, pytest_plan = plan(s.checks[0]), plan(s.checks[1])
    assert unittest_plan.argv == [
        "python",
        "-I",
        "-X",
        f"pycache_prefix={NOPYC}",
        "/eval/.agenteval/run_unittest.py",
        "test*.py",
    ]
    assert pytest_plan.argv[-2:] == ["/eval/.agenteval/run_pytest.py", "test*.py"]
    assert unittest_plan.stdin is None and unittest_plan.fresh_workspace
    assert NOPYC.startswith("/eval/")  # uncreatable: /eval is root-owned and read-only (C2)


def test_evaluator_files_are_hardened_and_valid_python() -> None:
    files = evaluator_files(build(PT, PY))
    assert set(files) == {".agenteval/run_unittest.py", ".agenteval/run_pytest.py", "conftest.py"}
    for source in files.values():
        compile(source, "<evaluator>", "exec")
    assert 'sys.path.append("/workspace")' in files[".agenteval/run_unittest.py"]
    assert "sys.path.insert" not in "".join(files.values())  # appended, never prepended (C2)
    runner = files[".agenteval/run_pytest.py"]
    for flag in (
        "--rootdir=/eval",
        "--confcutdir=/eval",
        "no:cacheprovider",
        "--ignore=/eval/.agenteval",
    ):
        assert flag in runner
    assert evaluator_files(build(ANS)) == {}


def test_io_and_command_plans_carry_stdin_and_env() -> None:
    io_plan, cmd_plan = plan(build(IO).checks[0]), plan(build(CMD).checks[0])
    assert (io_plan.argv, io_plan.stdin, io_plan.env) == (
        ["python", "solve.py"],
        "SECRET-IN",
        EXEC_ENV,
    )
    assert cmd_plan.env == EXEC_ENV and cmd_plan.stdin is None
    with pytest.raises(ValueError):
        plan(build(ANS).checks[0])


def test_static_checks_run_first_then_executing_in_declared_order() -> None:
    static, executing = execution_order(build(PT, ANS, IO, FILE, CMD))
    assert [c.name for c in static] == ["answer", "out"]
    assert [c.name for c in executing] == ["hidden", "io", "cmd"]


def test_file_paths_needed_include_protected_paths() -> None:
    s = build(FILE, ANS, protected_paths=["test_visible.py"])
    assert file_paths_needed(s) == ["out.txt", "test_visible.py"]


# -- judging executing checks (C3) ---------------------------------------------------------------


def out(exit_code: int | None = 0, stdout: str = "", stderr: str = "", **kw: Any) -> ExecOutput:
    return ExecOutput(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=120, **kw)


def test_python_tests_pass_only_with_exit_0_and_the_expected_count() -> None:
    check = build(PT).checks[0]
    ok = judge(check, out(0, stderr=UT_OK))
    assert (ok.passed, ok.score, ok.tests_passed, ok.tests_total, ok.failure) == (
        True,
        1.0,
        2,
        2,
        None,
    )
    failing = judge(check, out(1, stderr=UT_FAIL))
    assert (failing.passed, failing.score, failing.failure) == (
        False,
        0.5,
        FailureCategory.TESTS_FAILED,
    )


def test_os_exit_0_without_a_summary_is_not_a_pass() -> None:
    """The spike's naive attack: exit 0 at import, no tests ran."""
    r = judge(build(PT).checks[0], out(0))
    assert r.passed is False and r.score == 0.0 and "no test summary" in (r.actual or "")


def test_wrong_test_count_is_not_a_pass() -> None:
    """Hidden tests collected away (or extra ones injected): exit 0 isn't enough."""
    r = judge(build({**PT, "expected_tests": 5}).checks[0], out(0, stderr=UT_OK))
    assert r.passed is False and "expected 5" in r.detail


def test_nonzero_exit_with_an_ok_summary_is_not_a_pass() -> None:
    assert judge(build(PT).checks[0], out(1, stderr=UT_OK)).passed is False


def test_timeout_and_unknown_exit() -> None:
    check = build(PT).checks[0]
    timed_out = judge(check, out(124, timed_out=True))
    assert timed_out.passed is False and timed_out.failure is FailureCategory.TESTS_FAILED
    unknown = judge(check, out(None))
    assert unknown.passed is None and unknown.error  # an evaluator problem, not the agent's


def test_io_check_is_judged_from_exit_code_and_stdout() -> None:
    check = build(IO).checks[0]
    assert judge(check, out(0, stdout="5\n")).passed is True
    assert judge(check, out(0, stdout="6\n")).passed is False
    assert judge(check, out(1, stdout="5\n")).passed is False


def test_command_check() -> None:
    check = build(CMD).checks[0]
    assert judge(check, out(0)).passed is True
    assert judge(check, out(2)).passed is False


def test_executing_checks_never_record_process_output() -> None:
    """D11: tracebacks quote hidden tests; a candidate can echo hidden stdin."""
    leak = "LEAK-hidden-test-source-and-stdin"
    for spec_check in (PT, IO, CMD):
        r = judge(build(spec_check).checks[0], out(1, stdout=leak, stderr=leak + UT_FAIL))
        assert leak not in r.model_dump_json()


# -- judging static checks ------------------------------------------------------------------------


def test_final_answer() -> None:
    check = build(ANS).checks[0]
    assert judge_static(check, final_answer="42.0", evidence={}).passed is True
    wrong = judge_static(check, final_answer="41", evidence={})
    assert (wrong.passed, wrong.failure, wrong.actual) == (
        False,
        FailureCategory.WRONG_ANSWER,
        "41",
    )
    none = judge_static(check, final_answer=None, evidence={})
    assert (none.passed, none.failure) == (False, FailureCategory.NO_ANSWER)


def test_file_check_outcomes() -> None:
    check = build(FILE).checks[0]

    def judged(evidence: Any) -> Any:
        return judge_static(check, final_answer=None, evidence=evidence)

    assert judged({"out.txt": evidence_for("out.txt", b"hello\n")}).passed is True
    wrong = judged({"out.txt": evidence_for("out.txt", b"bye")})
    assert (wrong.passed, wrong.failure) == (False, FailureCategory.WRONG_ANSWER)
    missing = judged({"out.txt": evidence_for("out.txt", None)})
    assert (missing.passed, missing.failure) == (False, FailureCategory.MISSING_OUTPUT)
    assert judged({"out.txt": evidence_for("out.txt", b"hello\n", is_symlink=True)}).passed is False
    no_evidence = judged({})
    assert no_evidence.passed is None and no_evidence.error


@pytest.mark.parametrize(
    ("mode", "expected", "content", "ok"),
    [
        ("exists", None, b"", True),
        ("contains", "ell", b"hello", True),
        ("json_equals", '{"a": [1, 2]}', b'{ "a" : [1,2] }', True),
        ("json_equals", '{"a": 1}', b"not json", False),
        (
            "sha256",
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            b"hello",
            True,
        ),
    ],
)
def test_file_modes(mode: str, expected: str | None, content: bytes, ok: bool) -> None:
    check = build(
        {"kind": "file", "name": "f", "path": "f.txt", "mode": mode, "expected": expected}
    )
    r = judge_static(
        check.checks[0], final_answer=None, evidence={"f.txt": evidence_for("f.txt", content)}
    )
    assert r.passed is ok


def test_oversized_file_cannot_be_content_compared() -> None:
    big = evidence_for("out.txt", b"x" * 100, read_cap=10)
    assert big.content is None and big.sha256 is not None
    assert (
        judge_static(build(FILE).checks[0], final_answer=None, evidence={"out.txt": big}).passed
        is False
    )
