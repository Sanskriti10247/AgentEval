"""S5: platform, evaluator and candidate failures stay distinct end to end.

model-error  the model API fails (non-retryable 401)  -> run FAILED,
             evaluation SKIPPED platform_model, outside the denominator
hoarder      leaves 17 MiB of incompressible data      -> T1: run COMPLETED but
             the snapshot is too large: evaluation ERROR (A2), never the
             agent's grade; T3 (final answer only) is graded without a
             snapshot or container (A6) and passes
"""

import httpx
import pytest

from tests.e2e.bench import API, Batch, ok

pytestmark = pytest.mark.e2e


def test_s5_platform_failures_are_skipped_and_excluded(
    stack: httpx.Client, failures: dict[str, Batch]
) -> None:
    batch = failures["model-error"]
    for row in batch.rows:
        assert (row["run_status"], row["run_outcome"]) == ("FAILED", "MODEL_ERROR")
        ev = row["evaluation"]
        assert (ev["status"], ev["failure_category"], ev["task_success"]) == (
            "SKIPPED", "platform_model", None,
        )  # fmt: skip
        assert ev["score"] is None and ev["checks"] == []
    summary = ok(stack.get(f"{API}/evaluations/{batch.evaluation_id}/summary"), 200)
    assert (summary["attempts"], summary["graded"], summary["platform_failures"]) == (2, 0, 2)
    assert summary["platform_failure_rate"] == 1.0
    assert (summary["success_rate"], summary["ci95"], summary["mean_score"]) == (None, None, None)
    assert summary["failure_categories"] == {"platform_model": 2}


def test_s5_an_evaluator_failure_never_becomes_a_candidate_failure(
    stack: httpx.Client, failures: dict[str, Batch]
) -> None:
    batch = failures["hoarder"]
    cells = batch.by_label()
    t1, t3 = cells[("T1", 1)], cells[("T3", 1)]
    # The attempt itself succeeded; only grading couldn't happen (snapshot too large).
    assert (t1["run_status"], t1["run_outcome"]) == ("COMPLETED", "AGENT_FINISHED")
    assert t1["evaluation"]["status"] == "ERROR"
    assert t1["evaluation"]["failure_category"] == "evaluation_error"
    assert (t1["evaluation"]["task_success"], t1["evaluation"]["score"]) == (None, None)
    # A6: no workspace needed, so the same agent's other task is graded normally.
    assert (t3["evaluation"]["status"], t3["evaluation"]["failure_category"]) == (
        "SUCCEEDED", "passed",
    )  # fmt: skip
    summary = ok(stack.get(f"{API}/evaluations/{batch.evaluation_id}/summary"), 200)
    assert (summary["graded"], summary["passed"], summary["evaluation_errors"]) == (1, 1, 1)
    assert summary["success_rate"] == 1.0 and summary["platform_failures"] == 0
    assert summary["failure_categories"] == {"evaluation_error": 1, "passed": 1}
