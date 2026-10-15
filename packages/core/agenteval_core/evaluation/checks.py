"""Plan and judge each check kind. Pure: executing checks are planned here,
run by the Step 5 runner in the isolated eval container, and judged here
from the resulting ExecOutput.

Order (C5): non-executing checks (final_answer, file) first, then executing
checks in declared order, each on a freshly restored workspace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict

from agenteval_core.evaluation.matchers import load_json, match_text
from agenteval_core.evaluation.parsers import parse_pytest, parse_unittest
from agenteval_core.evaluation.results import (
    ANSWER_CHARS,
    CheckResult,
    ExecPlan,
    FileEvidence,
    clip,
)
from agenteval_core.evaluation.spec import (
    CheckSpec,
    CommandCheck,
    EvaluationSpec,
    FileCheck,
    FinalAnswerCheck,
    IoCheck,
    PythonTestsCheck,
)
from agenteval_core.evaluation.taxonomy import FailureCategory
from agenteval_core.hashing import sha256_hex
from agenteval_core.tools.workspace import ExecOutput

EVAL_DIR = "/eval"
WORKSPACE_DIR = "/workspace"
RUNNER_DIR = ".agenteval"  # under /eval, evaluator-owned
# C2: an uncreatable path under the root-owned /eval, so no bytecode cache is
# read or written at all (the spike forged a later hidden test's .pyc through
# a writable prefix).
NOPYC = f"{EVAL_DIR}/.nopyc"
PYTHON_HARDENED = ["python", "-I", "-X", f"pycache_prefix={NOPYC}"]
# Executing checks with author argv (io, command): Python honours these unless
# the author's argv passes -I/-E, in which case they should pass -X themselves.
EXEC_ENV = {"PYTHONPYCACHEPREFIX": NOPYC, "PYTHONDONTWRITEBYTECODE": "1"}

_UNITTEST_RUNNER = f'''\
import os, sys, unittest
os.chdir("{EVAL_DIR}")
sys.path.append("{WORKSPACE_DIR}")  # appended: the stdlib and site-packages win (C2)
suite = unittest.defaultTestLoader.discover(
    "{EVAL_DIR}", pattern=sys.argv[1], top_level_dir="{EVAL_DIR}"
)
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
'''
_PYTEST_RUNNER = f'''\
import os, sys
os.chdir("{EVAL_DIR}")
import pytest
sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir={EVAL_DIR}",
                      "--confcutdir={EVAL_DIR}", "--ignore={EVAL_DIR}/{RUNNER_DIR}",
                      "-o", "python_files=" + sys.argv[1], "{EVAL_DIR}"]))
'''
# Evaluator-owned conftest at the pytest rootdir; reserved in hidden_files.
_PYTEST_CONFTEST = f'import sys\nsys.path.append("{WORKSPACE_DIR}")  # appended (C2)\n'

STATIC_KINDS = frozenset({"final_answer", "file"})


def evaluator_files(spec: EvaluationSpec) -> dict[str, str]:
    """Files the evaluator places in /eval next to the hidden files (paths
    relative to /eval). Root-owned and read-only there, like hidden files."""
    frameworks = {c.framework for c in spec.checks if isinstance(c, PythonTestsCheck)}
    files: dict[str, str] = {}
    if "unittest" in frameworks:
        files[f"{RUNNER_DIR}/run_unittest.py"] = _UNITTEST_RUNNER
    if "pytest" in frameworks:
        files[f"{RUNNER_DIR}/run_pytest.py"] = _PYTEST_RUNNER
        files["conftest.py"] = _PYTEST_CONFTEST
    return files


def execution_order(spec: EvaluationSpec) -> tuple[list[CheckSpec], list[CheckSpec]]:
    """(static checks, executing checks), each in declared order (C5)."""
    static = [c for c in spec.checks if c.kind in STATIC_KINDS]
    executing = [c for c in spec.checks if c.kind not in STATIC_KINDS]
    return static, executing


def plan(check: CheckSpec) -> ExecPlan:
    if isinstance(check, PythonTestsCheck):
        runner = f"{EVAL_DIR}/{RUNNER_DIR}/run_{check.framework}.py"
        return ExecPlan(check.name, [*PYTHON_HARDENED, runner, check.pattern], check.timeout_s)
    if isinstance(check, IoCheck):
        return ExecPlan(
            check.name, list(check.argv), check.timeout_s, stdin=check.stdin, env=dict(EXEC_ENV)
        )
    if isinstance(check, CommandCheck):
        return ExecPlan(check.name, list(check.argv), check.timeout_s, env=dict(EXEC_ENV))
    raise ValueError(f"{check.kind} checks don't execute")


def judge(check: CheckSpec, out: ExecOutput) -> CheckResult:
    """Judge an executing check from its ExecOutput. Never records process
    output (D11), only exit code, counts, timing."""
    base = _base(check, duration_ms=out.duration_ms)
    if out.timed_out:
        return CheckResult(
            **base,
            passed=False,
            score=0.0,
            failure=FailureCategory.TESTS_FAILED,
            expected=_expected_summary(check),
            actual=f"timed out after {_timeout_s(check)}s",
            detail="the check ran past its timeout and was killed",
        )
    if out.exit_code is None:
        return CheckResult(**base, passed=None, error="could not determine the exit code")
    if isinstance(check, PythonTestsCheck):
        return _judge_tests(check, out, base)
    if isinstance(check, IoCheck):
        verdict = match_text(check.match, check.expected_stdout, out.stdout)
        exit_ok = out.exit_code == check.expected_exit_code
        passed = verdict.ok and exit_ok
        return CheckResult(
            **base,
            passed=passed,
            score=1.0 if passed else 0.0,
            failure=None if passed else FailureCategory.TESTS_FAILED,
            expected=_expected_summary(check),
            actual=f"exit {out.exit_code}; stdout {verdict.reason} ({len(out.stdout)} chars)",
            detail="output and exit code as expected"
            if passed
            else ("stdout " + verdict.reason if exit_ok else f"exit code {out.exit_code}"),
        )
    if isinstance(check, CommandCheck):
        passed = out.exit_code == check.expected_exit_code
        return CheckResult(
            **base,
            passed=passed,
            score=1.0 if passed else 0.0,
            failure=None if passed else FailureCategory.TESTS_FAILED,
            expected=_expected_summary(check),
            actual=f"exit {out.exit_code}",
            detail="trusted-environment command check (C6)",
        )
    raise ValueError(f"{check.kind} checks don't execute")


def judge_final_answer(check: FinalAnswerCheck, answer: str | None) -> CheckResult:
    base = _base(check)
    if not answer:
        return CheckResult(
            **base,
            passed=False,
            score=0.0,
            failure=FailureCategory.NO_ANSWER,
            expected=_expected_summary(check),
            actual=None,
            detail="the agent gave no final answer",
        )
    verdict = match_text(check.match, check.expected, answer, tolerance=check.tolerance)
    return CheckResult(
        **base,
        passed=verdict.ok,
        score=1.0 if verdict.ok else 0.0,
        failure=None if verdict.ok else FailureCategory.WRONG_ANSWER,
        expected=_expected_summary(check),
        actual=clip(answer, ANSWER_CHARS),
        detail="final answer " + verdict.reason,
    )


def judge_file(check: FileCheck, evidence: Mapping[str, FileEvidence]) -> CheckResult:
    base = _base(check)
    ev = evidence.get(check.path)
    if ev is None:
        return CheckResult(**base, passed=None, error=f"no evidence collected for {check.path}")

    def result(passed: bool, detail: str, failure: FailureCategory | None) -> CheckResult:
        return CheckResult(
            **base,
            passed=passed,
            score=1.0 if passed else 0.0,
            failure=None if passed else failure,
            expected=_expected_summary(check),
            actual=_file_facts(ev),
            detail=detail,
        )

    if not ev.exists:
        return result(False, f"{check.path} does not exist", FailureCategory.MISSING_OUTPUT)
    if ev.is_symlink:
        return result(False, f"{check.path} is a symlink, not a file", FailureCategory.WRONG_ANSWER)
    if check.mode == "exists":
        return result(True, f"{check.path} exists", None)
    assert check.expected is not None
    if check.mode == "sha256":
        ok = ev.sha256 == check.expected
        return result(
            ok, "content hash " + ("matches" if ok else "differs"), FailureCategory.WRONG_ANSWER
        )
    if ev.content is None:
        return result(False, f"{check.path} is too large to compare", FailureCategory.WRONG_ANSWER)
    text = ev.content.decode("utf-8", errors="replace")
    if check.mode == "json_equals":
        valid, got = load_json(text)
        ok = valid and got == load_json(check.expected)[1]
        return result(
            ok,
            "JSON " + ("equal" if ok else "differs" if valid else "invalid"),
            FailureCategory.WRONG_ANSWER,
        )
    mode = "exact" if check.mode == "equals" else "contains"
    verdict = match_text(mode, check.expected, text)
    return result(verdict.ok, "content " + verdict.reason, FailureCategory.WRONG_ANSWER)


def judge_static(
    check: CheckSpec, *, final_answer: str | None, evidence: Mapping[str, FileEvidence]
) -> CheckResult:
    if isinstance(check, FinalAnswerCheck):
        return judge_final_answer(check, final_answer)
    if isinstance(check, FileCheck):
        return judge_file(check, evidence)
    raise ValueError(f"{check.kind} checks execute; use plan()/judge()")


def file_paths_needed(spec: EvaluationSpec) -> list[str]:
    """Workspace paths the runner must collect FileEvidence for."""
    paths = {c.path for c in spec.checks if isinstance(c, FileCheck)} | set(spec.protected_paths)
    return sorted(paths)


def evidence_for(
    path: str, data: bytes | None, *, is_symlink: bool = False, read_cap: int = 64 * 1024
) -> FileEvidence:
    """Build FileEvidence from raw bytes (None = the path doesn't exist)."""
    if data is None:
        return FileEvidence(path=path, exists=False)
    return FileEvidence(
        path=path,
        exists=True,
        is_symlink=is_symlink,
        size=len(data),
        sha256=sha256_hex(data),
        content=data if len(data) <= read_cap else None,
    )


# -- internals ---------------------------------------------------------------------------


class _Base(TypedDict):
    name: str
    kind: str
    required: bool
    weight: float
    duration_ms: int


def _base(check: CheckSpec, *, duration_ms: int = 0) -> _Base:
    return {
        "name": check.name,
        "kind": check.kind,
        "required": check.required,
        "weight": check.weight,
        "duration_ms": duration_ms,
    }


def _judge_tests(check: PythonTestsCheck, out: ExecOutput, base: _Base) -> CheckResult:
    parse = parse_unittest if check.framework == "unittest" else parse_pytest
    counts = parse(out.stdout + "\n" + out.stderr)
    expected = _expected_summary(check)
    if counts is None:
        return CheckResult(
            **base,
            passed=False,
            score=0.0,
            failure=FailureCategory.TESTS_FAILED,
            expected=expected,
            actual=f"exit {out.exit_code}; no test summary",
            detail="no test summary found: not counted as a pass (C3)",
        )
    n = check.expected_tests
    passed = out.exit_code == 0 and counts.total == n and counts.all_passed
    if passed:
        detail = f"all {n} tests passed"
    elif counts.total != n:
        detail = f"ran {counts.total} tests, expected {n} (C3)"
    else:
        detail = f"{counts.failed} of {n} tests failed"
    return CheckResult(
        **base,
        passed=passed,
        score=round(min(counts.passed, n) / n, 5),
        failure=None if passed else FailureCategory.TESTS_FAILED,
        expected=expected,
        actual=f"exit {out.exit_code}; {counts.passed}/{counts.total} passed",
        detail=detail,
        tests_passed=counts.passed,
        tests_total=counts.total,
    )


def _expected_summary(check: CheckSpec) -> str:
    """Fixed templates: never the expected value itself (D11)."""
    if isinstance(check, PythonTestsCheck):
        return f"exit 0 and all {check.expected_tests} expected {check.framework} tests pass"
    if isinstance(check, IoCheck):
        return (
            f"exit {check.expected_exit_code} and stdout matching the expected output "
            f"({check.match})"
        )
    if isinstance(check, CommandCheck):
        return f"exit {check.expected_exit_code}"
    if isinstance(check, FinalAnswerCheck):
        return f"a final answer matching the expected answer ({check.match})"
    if isinstance(check, FileCheck):
        return f"{check.path}: {check.mode}"
    raise ValueError(check)


def _file_facts(ev: FileEvidence) -> str:
    if not ev.exists:
        return "missing"
    kind = "symlink" if ev.is_symlink else "file"
    return f"{kind}, {ev.size} bytes, sha256 {(ev.sha256 or '')[:12]}…"


def _timeout_s(check: CheckSpec) -> int:
    if isinstance(check, PythonTestsCheck | IoCheck | CommandCheck):
        return check.timeout_s
    raise ValueError(f"{check.kind} checks don't execute")


def required_checks(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.required]
