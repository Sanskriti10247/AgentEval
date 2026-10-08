"""Protected-path integrity and trajectory metrics."""

from typing import Any

import pytest

from agenteval_core.agent.events import EventType, TrajectoryEventData
from agenteval_core.evaluation.checks import evidence_for
from agenteval_core.evaluation.integrity import assess, baseline
from agenteval_core.evaluation.metrics import compute

TASK_FILES = {"test_visible.py": "import unittest\n", "calc.py": "def add(a, b): return a - b\n"}


def test_untouched_protected_files_are_ok() -> None:
    expected = baseline(TASK_FILES, ["test_visible.py"])
    ev = {
        "test_visible.py": evidence_for("test_visible.py", TASK_FILES["test_visible.py"].encode())
    }
    assert assess(expected, ev).ok


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (evidence_for("test_visible.py", b"import unittest  # edited\n"), "content changed"),
        (evidence_for("test_visible.py", None), "missing"),
        (
            evidence_for(
                "test_visible.py", TASK_FILES["test_visible.py"].encode(), is_symlink=True
            ),
            "replaced by a symlink",
        ),
    ],
)
def test_tampering_is_detected(evidence: Any, reason: str) -> None:
    result = assess(baseline(TASK_FILES, ["test_visible.py"]), {"test_visible.py": evidence})
    assert not result.ok and result.tampered == {"test_visible.py": reason}


def test_missing_evidence_is_a_runner_bug_not_clean_integrity() -> None:
    with pytest.raises(KeyError):
        assess(baseline(TASK_FILES, ["test_visible.py"]), {})


def test_normalized_paths_are_recorded_not_tampering() -> None:
    expected = baseline(TASK_FILES, ["test_visible.py"])
    ev = {
        "test_visible.py": evidence_for("test_visible.py", TASK_FILES["test_visible.py"].encode())
    }
    result = assess(expected, ev, normalized_paths=["z.txt", "a.txt"])
    assert result.ok and result.normalized_paths == ["a.txt", "z.txt"]


# -- metrics -----------------------------------------------------------------------------------


def ev(seq: int, kind: EventType, **kw: Any) -> TrajectoryEventData:
    return TrajectoryEventData(seq=seq, step=0, event_type=kind, **kw)


def tool(
    seq: int, name: str, status: str = "ok", inp: dict[str, Any] | None = None, content: str = ""
) -> TrajectoryEventData:
    return ev(
        seq,
        EventType.TOOL_CALL,
        tool=name,
        status=status,
        input=inp or {},
        output={"content": content},
    )


def test_metrics_from_a_typical_run() -> None:
    events = [
        ev(0, EventType.RUN_STARTED),
        ev(1, EventType.MODEL_ERROR, error_class="overloaded"),
        ev(2, EventType.MODEL_CALL),
        tool(3, "run_tests", content="Tests failed (exit 1).\n..."),
        tool(4, "read_file", status="invalid_input"),
        tool(5, "write_file", inp={"path": "calc.py"}),
        ev(6, EventType.MODEL_CALL),
        tool(7, "run_tests", content="Tests passed.\nOK"),
        ev(8, EventType.MODEL_CALL),
        ev(9, EventType.RUN_FINISHED),
    ]
    m = compute(events)
    assert (m.model_calls, m.model_errors, m.tool_calls, m.agent_tool_errors) == (3, 1, 4, 1)
    assert m.tool_error_rate == 0.25 and m.tool_status_counts == {"invalid_input": 1, "ok": 3}
    assert (m.ran_tests, m.last_tests_passed, m.loop_detected) == (True, True, False)


def test_loop_detection_needs_identical_consecutive_calls() -> None:
    same = [tool(i, "read_file", inp={"path": "a.py"}) for i in range(3)]
    assert compute(same).loop_detected and compute(same).max_identical_streak == 3
    varied = [tool(i, "read_file", inp={"path": f"{i}.py"}) for i in range(5)]
    assert not compute(varied).loop_detected
    interrupted = same[:2] + [tool(9, "list_dir")] + same[:1]
    assert compute(interrupted).max_identical_streak == 2


def test_no_tool_calls_and_limits() -> None:
    m = compute(
        [ev(0, EventType.MODEL_CALL), ev(1, EventType.LIMIT_REACHED, output={"limit": "max_steps"})]
    )
    assert (m.tool_calls, m.tool_error_rate, m.ran_tests, m.last_tests_passed) == (
        0,
        None,
        False,
        None,
    )
    assert m.limits_reached == ["max_steps"]


def test_metrics_accept_stored_rows_with_plain_string_types() -> None:
    class Row:  # shaped like the TrajectoryEvent ORM row
        def __init__(self, event_type: str, **kw: Any) -> None:
            self.event_type, self.tool, self.status = event_type, kw.get("tool"), kw.get("status")
            self.input, self.output = kw.get("input"), kw.get("output")

    m = compute(
        [
            Row("model_call"),
            Row("tool_call", tool="run_tests", status="ok", output={"content": "Tests passed."}),
        ]
    )
    assert (m.model_calls, m.tool_calls, m.last_tests_passed) == (1, 1, True)
