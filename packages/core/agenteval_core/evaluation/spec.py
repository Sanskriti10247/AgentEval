"""The evaluation spec stored in tasks.evaluation.

INTERNAL evaluator data (D11): checks, expected values and hidden files are
never returned by the API or shown to the model. Hidden files are only ever
placed in the isolated evaluation container's /eval.

Check kinds:
  python_tests   hidden unittest/pytest files run by an evaluator-generated,
                 hardened invocation (C2); passes only on exit 0 AND the
                 parsed run count == expected_tests with no failures (C3)
  io             run the candidate as its own process with stdin; the worker
                 compares stdout/exit code OUTSIDE the sandbox (C4) - the
                 robust choice when integrity matters most
  command        a free-form command; trusted-environment only (C6/C7) and
                 must say so explicitly
  final_answer   compare the agent's final answer
  file           compare a file the agent left in /workspace

Plus protected_paths: task files whose content must not change (tampering).
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenteval_core.evaluation.matchers import load_json, parse_number
from agenteval_core.hashing import config_hash_of
from agenteval_core.schemas.task import MAX_FILES, MAX_FILES_BYTES, validate_relative_path

MAX_CHECKS = 20
MAX_EXPECTED_BYTES = 64 * 1024
MAX_STDIN_BYTES = 64 * 1024
MAX_ARGS = 64
MAX_ARG_LEN = 4096
# Names in /eval the evaluator owns; hidden files can't use them.
RESERVED_EVAL_NAMES = frozenset({".agenteval", ".nopyc", "conftest.py"})
# C6: argv[0] must be a bare program name (an interpreter), never a path: the
# sandbox mounts are noexec and a workspace path would run agent-controlled code.
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _argv(v: list[str]) -> list[str]:
    if not v:
        raise ValueError("argv must not be empty")
    if not _PROGRAM.match(v[0]):
        raise ValueError(
            f"argv[0] must be a bare program name such as 'python' (no paths): {v[0]!r}"
        )
    if len(v) > MAX_ARGS or any(len(a) > MAX_ARG_LEN or "\x00" in a for a in v):
        raise ValueError(f"at most {MAX_ARGS} args of <= {MAX_ARG_LEN} chars, no NUL")
    return v


def _bounded_text(v: str, what: str, limit: int) -> str:
    if len(v.encode()) > limit:
        raise ValueError(f"{what} exceeds {limit} bytes")
    return v


class _CheckBase(_Spec):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    weight: float = Field(default=1.0, gt=0, le=100)
    # task_success requires every required check to pass.
    required: bool = True


class PythonTestsCheck(_CheckBase):
    kind: Literal["python_tests"] = "python_tests"
    framework: Literal["unittest", "pytest"] = "unittest"
    pattern: str = Field(default="test*.py", pattern=r"^[A-Za-z0-9_*?.-]{1,64}$")
    expected_tests: int = Field(ge=1, le=10_000)
    timeout_s: int = Field(default=60, ge=1, le=300)


MatchMode = Literal["exact", "normalized", "contains", "regex"]


class IoCheck(_CheckBase):
    kind: Literal["io"] = "io"
    argv: list[str]
    stdin: str = ""
    expected_stdout: str
    match: MatchMode = "normalized"
    expected_exit_code: int = 0
    timeout_s: int = Field(default=30, ge=1, le=300)

    @field_validator("argv")
    @classmethod
    def _v_argv(cls, v: list[str]) -> list[str]:
        return _argv(v)

    @field_validator("stdin")
    @classmethod
    def _v_stdin(cls, v: str) -> str:
        return _bounded_text(v, "stdin", MAX_STDIN_BYTES)

    @field_validator("expected_stdout")
    @classmethod
    def _v_expected(cls, v: str) -> str:
        return _bounded_text(v, "expected_stdout", MAX_EXPECTED_BYTES)

    @model_validator(mode="after")
    def _v_regex(self) -> IoCheck:
        if self.match == "regex":
            _compile(self.expected_stdout)
        return self


class CommandCheck(_CheckBase):
    """C6/C7: no in-process integrity guarantees. The spec must acknowledge it."""

    kind: Literal["command"] = "command"
    argv: list[str]
    expected_exit_code: int = 0
    trusted_environment: Literal[True]
    timeout_s: int = Field(default=60, ge=1, le=300)

    @field_validator("argv")
    @classmethod
    def _v_argv(cls, v: list[str]) -> list[str]:
        return _argv(v)


class FinalAnswerCheck(_CheckBase):
    kind: Literal["final_answer"] = "final_answer"
    match: Literal["exact", "normalized", "contains", "regex", "number", "json_equals"] = (
        "normalized"
    )
    expected: str
    tolerance: float = Field(default=0.0, ge=0)  # number mode: absolute tolerance

    @field_validator("expected")
    @classmethod
    def _v_expected(cls, v: str) -> str:
        return _bounded_text(v, "expected", MAX_EXPECTED_BYTES)

    @model_validator(mode="after")
    def _v_mode(self) -> FinalAnswerCheck:
        if self.match == "regex":
            _compile(self.expected)
        elif self.match == "number" and parse_number(self.expected) is None:
            raise ValueError("number mode needs a numeric expected value")
        elif self.match == "json_equals" and not load_json(self.expected)[0]:
            raise ValueError("json_equals mode needs valid JSON as the expected value")
        return self


class FileCheck(_CheckBase):
    kind: Literal["file"] = "file"
    path: str
    mode: Literal["exists", "equals", "contains", "sha256", "json_equals"] = "exists"
    expected: str | None = None

    @field_validator("path")
    @classmethod
    def _v_path(cls, v: str) -> str:
        validate_relative_path(v)
        return v

    @model_validator(mode="after")
    def _v_expected(self) -> FileCheck:
        if self.mode == "exists":
            if self.expected is not None:
                raise ValueError("mode 'exists' takes no expected value")
            return self
        if self.expected is None:
            raise ValueError(f"mode {self.mode!r} needs an expected value")
        _bounded_text(self.expected, "expected", MAX_EXPECTED_BYTES)
        if self.mode == "sha256" and not re.fullmatch(r"[0-9a-f]{64}", self.expected):
            raise ValueError("sha256 mode needs a lowercase hex digest")
        if self.mode == "json_equals" and not load_json(self.expected)[0]:
            raise ValueError("json_equals mode needs valid JSON as the expected value")
        return self


CheckSpec = Annotated[
    PythonTestsCheck | IoCheck | CommandCheck | FinalAnswerCheck | FileCheck,
    Field(discriminator="kind"),
]
EXECUTING_KINDS = frozenset({"python_tests", "io", "command"})


class EvaluationSpec(_Spec):
    checks: list[CheckSpec] = Field(min_length=1, max_length=MAX_CHECKS)
    hidden_files: dict[str, str] = Field(default_factory=dict, max_length=MAX_FILES)
    protected_paths: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    # Wall-clock budget for the whole evaluation (all checks).
    timeout_s: int = Field(default=120, ge=10, le=600)

    @field_validator("hidden_files")
    @classmethod
    def _v_hidden(cls, v: dict[str, str]) -> dict[str, str]:
        total = 0
        for path, content in v.items():
            validate_relative_path(path)
            if path.split("/")[0] in RESERVED_EVAL_NAMES:
                raise ValueError(f"hidden file {path!r} uses a name reserved by the evaluator")
            total += len(path.encode()) + len(content.encode())
        if total > MAX_FILES_BYTES:
            raise ValueError(f"hidden files exceed {MAX_FILES_BYTES} bytes in total")
        return v

    @field_validator("protected_paths")
    @classmethod
    def _v_protected(cls, v: list[str]) -> list[str]:
        for path in v:
            validate_relative_path(path)
        if len(set(v)) != len(v):
            raise ValueError("protected_paths must be unique")
        return v

    @model_validator(mode="after")
    def _v_checks(self) -> EvaluationSpec:
        names = [c.name for c in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must be unique")
        if not any(c.required for c in self.checks):
            raise ValueError("at least one check must be required")
        for c in self.checks:
            if isinstance(c, PythonTestsCheck):
                matches = [
                    p
                    for p in self.hidden_files
                    if fnmatch.fnmatch(posixpath.basename(p), c.pattern)
                ]
                if not matches:
                    raise ValueError(
                        f"check {c.name!r}: no hidden file matches pattern {c.pattern!r}"
                    )
        budget = sum(getattr(c, "timeout_s", 0) for c in self.checks)
        if budget > self.timeout_s:
            raise ValueError(
                f"per-check timeouts ({budget}s) exceed the evaluation timeout ({self.timeout_s}s)"
            )
        return self

    def spec_hash(self) -> str:
        """Identity of the exact spec (incl. hidden files), for reproducibility."""
        return config_hash_of(self.model_dump(mode="json"))


def validate_against_task(spec: EvaluationSpec, task_files: dict[str, str]) -> None:
    """Cross-checks that need the task: protected paths must be task files
    (their original content is the integrity baseline). Raises ValueError."""
    missing = [p for p in spec.protected_paths if p not in task_files]
    if missing:
        raise ValueError(f"protected_paths are not task files: {missing}")


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
