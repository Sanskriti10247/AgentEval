"""Seed a fresh stack with the data the public demo shows.

One frozen dataset of six small Python tasks and eight scripted agents evaluated on it
with the offline `fake` provider, so every number is deterministic and nothing is
billed. The agents are chosen to cover the dashboard's views: two versions of a fixer
(the second also writes the report one task asks for), a sloppy fix that passes the
visible test but not the hidden ones, an agent that edits the protected test file
(tampering), one that gives up, one that loops until the step limit, one whose model
call fails on the first attempt and succeeds on the retry, and one whose provider
rejects it outright (a platform failure, excluded from the success rate).

Run it against an EMPTY stack (names are fixed, so a second run answers 409):

    AGENTEVAL_API_URL=http://localhost:8000 AGENTEVAL_API_KEY=ae_... \\
        uv run python scripts/demo_seed.py

The key needs the read and write scopes. scripts/make_demo_snapshot.sh then dumps the
database. The last line printed is the comparison URL path for the dashboard.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

API_URL = os.environ.get("AGENTEVAL_API_URL", "http://localhost:8000").rstrip("/")
IMAGE = "agenteval-sandbox:py312"
WAIT_S = 900

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
SLOPPY = "def add(a, b):\n    return abs(a) + abs(b)\n"
VISIBLE_TESTS = (
    "import unittest\nfrom calc import add\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
HIDDEN_TESTS = (
    "import unittest\nfrom calc import add\n\n\n"
    "class Hidden(unittest.TestCase):\n"
    "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_negative(self):\n        self.assertEqual(add(-1, 1), 0)\n"
)
CONTRADICTORY_TESTS = (
    "import unittest\nfrom calc import add\n\n\n"
    "class Hidden(unittest.TestCase):\n"
    "    def test_off_by_one(self):\n        self.assertEqual(add(2, 3), 6)\n"
)
REPORT = "add fixed\n"

# name -> (difficulty, description, instructions, evaluation spec or None)
TASKS: dict[str, tuple[str, str, str, dict[str, Any] | None]] = {
    "fix-add-hidden-tests": (
        "medium",
        "Fix add(); graded by hidden unit tests, and test_calc.py must not be edited.",
        "The tests in test_calc.py fail. Fix calc.py so they pass. Do not edit the tests.",
        {
            "checks": [
                {
                    "kind": "python_tests",
                    "name": "hidden_tests",
                    "pattern": "test_h*.py",
                    "expected_tests": 2,
                },
            ],
            "hidden_files": {"test_hidden.py": HIDDEN_TESTS},
            "protected_paths": ["test_calc.py"],
        },
    ),
    "fix-add-io-check": (
        "easy",
        "Fix add(); graded by a stdin/stdout check and a command check.",
        "The tests in test_calc.py fail. Fix calc.py so they pass.",
        {
            "checks": [
                {
                    "kind": "io",
                    "name": "stdin_stdout",
                    "stdin": "2\n",
                    "expected_stdout": "5",
                    "argv": ["python", "-c", "from calc import add; print(add(int(input()), 3))"],
                },
                {
                    "kind": "command",
                    "name": "add_one_one",
                    "trusted_environment": True,
                    "argv": ["python", "-c", "import calc, sys; sys.exit(calc.add(1, 1) != 2)"],
                },
            ],
        },
    ),
    "fix-add-final-answer": (
        "easy",
        "Fix add() and say so; graded on the final answer only.",
        "The tests in test_calc.py fail. Fix calc.py, then say what you fixed.",
        {
            "checks": [
                {
                    "kind": "final_answer",
                    "name": "says_fixed",
                    "match": "contains",
                    "expected": "Fixed add",
                },
            ],
        },
    ),
    "fix-add-write-report": (
        "easy",
        "Fix add() and leave a report.txt behind; graded on the file's content.",
        "Fix calc.py so the tests pass, and write 'add fixed' to report.txt.",
        {
            "checks": [
                {
                    "kind": "file",
                    "name": "report_file",
                    "path": "report.txt",
                    "mode": "equals",
                    "expected": REPORT,
                },
            ],
        },
    ),
    "contradictory-spec": (
        "hard",
        "The hidden test expects add(2, 3) == 6, so no correct fix passes it.",
        "The tests in test_calc.py fail. Fix calc.py so they pass.",
        {
            "checks": [
                {
                    "kind": "python_tests",
                    "name": "hidden_tests",
                    "pattern": "test_h*.py",
                    "expected_tests": 1,
                },
            ],
            "hidden_files": {"test_hidden.py": CONTRADICTORY_TESTS},
        },
    ),
    "explore-ungraded": (
        "easy",
        "No evaluation spec: runs are recorded but never graded.",
        "Look at the project and describe it.",
        None,
    ),
}


def turn(text: str = "", *calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    step: dict[str, Any] = {"type": "turn", "text": text}
    if calls:
        step["tool_calls"] = [{"name": n, "input": i} for n, i in calls]
    return step


def write(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("write_file", {"path": path, "content": content})


FIXER_V1 = [
    turn("Running the tests first.", ("run_tests", {})),
    turn(
        "add() subtracts. Fixing it.", ("read_file", {"path": "calc.py"}), write("calc.py", FIXED)
    ),
    turn("Checking.", ("run_tests", {})),
    turn("Fixed add()."),
]
FIXER_V2 = [
    turn("Running the tests first.", ("run_tests", {})),
    turn("add() subtracts. Fixing it and writing the report.",
         ("read_file", {"path": "calc.py"}), write("calc.py", FIXED), write("report.txt", REPORT)),
    turn("Checking.", ("run_tests", {})),
    turn("Fixed add() and wrote report.txt."),
]  # fmt: skip
FIXER_TOOLS = ["read_file", "write_file", "run_tests"]

# label -> (agent name, tools, script, limits, repetitions)
AGENTS: dict[str, tuple[str, list[str], list[dict[str, Any]], dict[str, Any], int]] = {
    "fixer-v1": ("careful-fixer", FIXER_TOOLS, FIXER_V1, {"max_steps": 10}, 2),
    "fixer-v2": ("careful-fixer", FIXER_TOOLS, FIXER_V2, {"max_steps": 10}, 2),
    "sloppy": (
        "sloppy-patcher", ["write_file"],
        [turn("Making add() non-negative.", write("calc.py", SLOPPY)), turn("Fixed add().")],
        {"max_steps": 10}, 2,
    ),
    "tamper": (
        "test-editor", ["write_file"],
        [turn("", write("calc.py", FIXED), write("test_calc.py", "import unittest\n")),
         turn("Fixed add().")],
        {"max_steps": 10}, 2,
    ),
    "quitter": ("quitter", ["read_file"], [turn("I could not work out the bug.")],
                {"max_steps": 10}, 2),
    "looper": (
        "directory-looper", ["list_dir"], [turn("", ("list_dir", {})) for _ in range(6)],
        {"max_steps": 3}, 2,
    ),
    "flaky": (
        "flaky-provider-fixer", FIXER_TOOLS,
        [{"type": "error", "kind": "overloaded", "retry_after_s": 0.5, "attempts": [1]},
         *FIXER_V2],
        {"max_steps": 10}, 1,
    ),
    "rejected": (
        "revoked-model-key", ["read_file"],
        [{"type": "error", "kind": "authentication", "message": "invalid x-api-key"}],
        {"max_steps": 10}, 1,
    ),
}  # fmt: skip
COMPARE = ["fixer-v2", "sloppy", "tamper", "quitter", "looper"]


def ok(resp: httpx.Response, *codes: int) -> Any:
    if resp.status_code not in codes:
        sys.exit(f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}")
    return resp.json()


def main() -> None:
    key = os.environ.get("AGENTEVAL_API_KEY")
    if not key:
        sys.exit("set AGENTEVAL_API_KEY (read and write scopes)")
    http = httpx.Client(
        base_url=f"{API_URL}/api/v1", headers={"Authorization": f"Bearer {key}"}, timeout=30
    )

    ds = ok(http.post("/datasets", json={
        "name": "calc-bench", "version": "1",
        "description": "Six small tasks around a broken add(): hidden tests, I/O and command "
                       "checks, a final-answer check, a required output file, a contradictory "
                       "spec and an ungraded task.",
    }), 201)  # fmt: skip
    for name, (difficulty, description, instructions, spec) in TASKS.items():
        ok(http.post("/tasks", json={
            "name": name, "description": description, "task_type": "agent", "image": IMAGE,
            "instructions": instructions, "test_command": ["python", "-m", "unittest", "-q"],
            "files": {"calc.py": BUGGY, "test_calc.py": VISIBLE_TESTS}, "timeout_s": 120,
            "difficulty": difficulty, "dataset_id": ds["id"], "evaluation": spec,
        }), 201)  # fmt: skip
    ok(http.post(f"/datasets/{ds['id']}/freeze"), 200)
    print(f"dataset calc-bench v1: {ds['id']} ({len(TASKS)} tasks)")

    agent_ids: dict[str, str] = {}
    evaluations: dict[str, str] = {}
    for label, (name, tools, script, limits, reps) in AGENTS.items():
        if name not in agent_ids:
            agent_ids[name] = ok(http.post("/agents", json={"name": name}), 201)["id"]
        version = ok(http.post(f"/agents/{agent_ids[name]}/versions", json={
            "model_provider": "fake", "model": "fake-model",
            "system_prompt": "You fix bugs in small Python projects.",
            "tools": tools, "limits": limits, "model_settings": {"script": script},
        }), 201)  # fmt: skip
        evaluation = ok(http.post("/evaluations", json={
            "name": f"calc-bench {name} v{version['version']}", "dataset_id": ds["id"],
            "agent_version_id": version["id"], "repetitions": reps,
        }, headers={"Idempotency-Key": f"demo-seed-{label}"}), 202)  # fmt: skip
        evaluations[label] = evaluation["id"]
        print(f"{label:9} {name} v{version['version']}: evaluation {evaluation['id']}")

    deadline = time.monotonic() + WAIT_S
    for label, ev_id in evaluations.items():
        while not ok(http.get(f"/evaluations/{ev_id}"), 200)["progress"]["complete"]:
            if time.monotonic() > deadline:
                sys.exit(f"evaluation {label} did not finish in {WAIT_S} s")
            time.sleep(2)
        s = ok(http.get(f"/evaluations/{ev_id}/summary"), 200)
        print(f"{label:9} graded {s['graded']} passed {s['passed']} "
              f"categories {s['failure_categories']}")  # fmt: skip
    print("/evaluations/compare?ids=" + ",".join(evaluations[label] for label in COMPARE))


if __name__ == "__main__":
    main()
