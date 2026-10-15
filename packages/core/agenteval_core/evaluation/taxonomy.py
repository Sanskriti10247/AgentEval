"""Evaluation vocabularies shared by the schema (CHECK constraints are derived
from these, like states.py for run outcomes) and, from Step 3, the evaluator.
"""

from enum import StrEnum


class DatasetStatus(StrEnum):
    DRAFT = "DRAFT"  # tasks can still be added
    FROZEN = "FROZEN"  # immutable; evaluations require a frozen dataset


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class EvaluationStatus(StrEnum):
    """Lifecycle of one attempt's evaluation. Separate from the run's status
    (D1): the run is already terminal when evaluation starts."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"  # checks ran; task_success and a category are set
    ERROR = "ERROR"  # the evaluator failed; the attempt is untouched (re-checkable later)
    SKIPPED = "SKIPPED"  # not graded: platform failure, or the task has no evaluation spec


class FailureCategory(StrEnum):
    """One primary category per evaluated attempt (D10). Precedence is applied
    by the classifier (Step 3), top to bottom."""

    PASSED = "passed"
    # Platform: never fairly tested; excluded from the success denominator (D7).
    PLATFORM_SANDBOX = "platform_sandbox"
    PLATFORM_MODEL = "platform_model"
    PLATFORM_INTERNAL = "platform_internal"
    PLATFORM_CANCELLED = "platform_cancelled"
    # The evaluator itself failed.
    EVALUATION_ERROR = "evaluation_error"
    # Integrity.
    TAMPERING = "tampering"
    # Limits.
    MAX_STEPS = "max_steps"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"
    # Agent behaviour.
    REFUSAL = "refusal"
    NO_ANSWER = "no_answer"
    # Task result.
    TESTS_FAILED = "tests_failed"
    WRONG_ANSWER = "wrong_answer"
    MISSING_OUTPUT = "missing_output"


PLATFORM_CATEGORIES: frozenset[FailureCategory] = frozenset(
    c for c in FailureCategory if c.value.startswith("platform_")
)


class DiagnosticTag(StrEnum):
    """How the agent worked; tags only, never a primary category."""

    LOOP_DETECTED = "loop_detected"
    HIGH_TOOL_ERROR_RATE = "high_tool_error_rate"
    NEVER_RAN_TESTS = "never_ran_tests"
    MODEL_RETRIES = "model_retries"
    PERMISSIONS_NORMALIZED = "permissions_normalized"


# Every tag a verdict may carry: secondary categories plus diagnostics. A closed
# vocabulary, so nothing free-form can travel through tags (D11).
TAG_VOCABULARY: frozenset[str] = frozenset(
    {c.value for c in FailureCategory} | {d.value for d in DiagnosticTag}
)


class ArtifactKind(StrEnum):
    WORKSPACE = "workspace"  # tar.gz of /workspace after the agent finished (D3)


# D3: snapshots are capped at 16 MiB compressed.
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
# Upper bound on a task's stored evaluation spec (checks + hidden files). The
# API enforces the 1 MB hidden-file limit; this is the database backstop.
MAX_EVALUATION_SPEC_BYTES = 2 * 1024 * 1024
