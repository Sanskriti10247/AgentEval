"""Evaluation data structures.

INTERNAL evaluator records (D11). CheckResult, IntegrityResult, ExecPlan and
FileEvidence may carry evaluator-only configuration - check names, paths,
expected test counts, match modes, argv, stdin - so they are persisted for
the evaluator but are NEVER returned by the API or shown to the model.
public.public_view() is the only projection allowed out.

Defence in depth even internally: no field ever holds hidden file contents or
expected values verbatim (expected values appear only as fixed-template
summaries), and executing checks never record process output - a failing
hidden test's traceback quotes the hidden test file, and a candidate can echo
its hidden stdin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from agenteval_core.evaluation.taxonomy import FailureCategory
from agenteval_core.states import Outcome

SUMMARY_CHARS = 500  # internal summaries
ANSWER_CHARS = 2000  # agent-produced text kept in the internal record


def clip(text: str, limit: int = SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    required: bool
    weight: float
    passed: bool | None  # None: the check could not run (see error)
    score: float | None = Field(default=None, ge=0, le=1)
    expected: str | None = None  # internal template summary, never the value itself
    actual: str | None = None  # agent-produced or derived facts; no process output
    detail: str = ""
    error: str | None = None  # the evaluator failed to run this check
    # Which task-result category a failure of this check means (classifier input).
    failure: FailureCategory | None = None
    tests_passed: int | None = None
    tests_total: int | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class FileEvidence:
    """What the runner observed about one workspace path (Step 5 fills it)."""

    path: str
    exists: bool
    is_symlink: bool = False
    size: int | None = None
    sha256: str | None = None
    content: bytes | None = None  # None if absent or above the read cap


class IntegrityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    tampered: dict[str, str] = Field(default_factory=dict)  # path -> reason
    # C1: paths whose owner read/search bits were normalised before export.
    normalized_paths: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class AttemptFacts:
    """What the evaluator knows about the finished attempt (from the DB)."""

    outcome: Outcome
    error_message: str | None = None
    final_answer: str | None = None
    steps: int | None = None
    cost_usd: Decimal | None = None
    max_steps: int | None = None
    max_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class ExecPlan:
    """One executing check, ready for the runner (Step 5) to run in the eval
    container: argv runs with cwd /workspace unless the argv itself changes
    directory. `fresh_workspace` asks for /workspace to be restored from the
    snapshot and /tmp wiped first (C5)."""

    check_name: str
    argv: list[str]
    timeout_s: int
    stdin: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    fresh_workspace: bool = True
