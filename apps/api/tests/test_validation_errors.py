"""D11 / R2: request validation errors never echo submitted content, and
errors inside a task's evaluation spec reveal nothing about it. Runs the real
handler and the real TaskCreate model; no database."""

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agenteval_api.errors import install_error_handlers, sanitize_error
from agenteval_core.evaluation.spec import MAX_EXPECTED_BYTES
from agenteval_core.schemas.task import MAX_FILES_BYTES
from agenteval_core.schemas.task_api import TaskCreate

SECRET = "SECRET_7d1e"


def make_client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/tasks")
    def create(body: TaskCreate) -> dict[str, str]:
        return {}

    return TestClient(app)


def task(evaluation: Any, **kw: Any) -> dict[str, Any]:
    return {
        "name": "t",
        "task_type": "agent",
        "image": "img",
        "instructions": "do",
        "evaluation": evaluation,
        **kw,
    }


TESTS = {"kind": "python_tests", "name": f"chk_{SECRET}", "expected_tests": 1}
BAD_SPECS = {
    # hidden file name and content in a failing hidden_files validator
    "reserved hidden file": {"checks": [TESTS], "hidden_files": {"conftest.py": SECRET}},
    "traversal file name": {"checks": [TESTS], "hidden_files": {f"../{SECRET}.py": SECRET}},
    # check name + pattern quoted by a model validator
    "pattern matches nothing": {
        "checks": [{**TESTS, "pattern": f"x_{SECRET}*.py"}],
        "hidden_files": {"test_a.py": SECRET},
    },
    "duplicate check names": {
        "checks": [TESTS, TESTS],
        "hidden_files": {"test_a.py": SECRET},
    },
    # an expected value in a failing field validator
    "invalid regex": {
        "checks": [
            {"kind": "final_answer", "name": "a", "match": "regex", "expected": f"({SECRET}"}
        ],
    },  # fmt: skip
    "unknown field": {"checks": [TESTS], "hidden_files": {"test_a.py": SECRET}, SECRET: 1},
    "wrong type deep inside": {
        "checks": [
            {
                "kind": "io",
                "name": "i",
                "argv": ["python"],
                "expected_stdout": "",
                "stdin": {SECRET: SECRET},
            }
        ],
    },  # fmt: skip
    "oversized expected value": {
        "checks": [
            {
                "kind": "final_answer",
                "name": "a",
                "expected": SECRET * (MAX_EXPECTED_BYTES // len(SECRET) + 1),
            }
        ],
    },  # fmt: skip
    "oversized hidden files": {
        "checks": [TESTS],
        "hidden_files": {"test_a.py": SECRET * (MAX_FILES_BYTES // len(SECRET) + 1)},
    },
}


@pytest.mark.parametrize("spec", list(BAD_SPECS.values()), ids=list(BAD_SPECS))
def test_invalid_specs_reveal_nothing(spec: dict[str, Any]) -> None:
    resp = make_client().post("/tasks", json=task(spec))
    assert resp.status_code == 422
    assert SECRET not in resp.text
    for error in resp.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}
        assert error["msg"].startswith("invalid evaluation spec (")
        assert error["loc"][:2] == ["body", "evaluation"]
        assert len(error["loc"]) <= 4
        if len(error["loc"]) == 4:
            assert isinstance(error["loc"][3], int)


def test_errors_outside_the_spec_keep_their_message_but_never_the_input() -> None:
    resp = make_client().post("/tasks", json=task(None, name="", files={"../x": SECRET}))
    assert resp.status_code == 422
    assert SECRET not in resp.text
    errors = resp.json()["detail"]
    assert all(set(e) == {"type", "loc", "msg"} for e in errors)
    assert any(e["loc"] == ["body", "name"] and "at least 1" in e["msg"] for e in errors)


def test_malformed_json_bodies_are_sanitised_too() -> None:
    raw = '{"evaluation": {"hidden_files": {"a.py": "' + SECRET + '"'  # truncated JSON
    resp = make_client().post("/tasks", content=raw, headers={"content-type": "application/json"})
    assert resp.status_code == 422
    assert SECRET not in resp.text


SPEC_LOC = ("body", "evaluation")
LOCATION_RULES = [
    (SPEC_LOC, ["body", "evaluation"]),
    ((*SPEC_LOC, "hidden_files", f"{SECRET}.py"), ["body", "evaluation", "hidden_files"]),
    ((*SPEC_LOC, "checks", 2, "python_tests", "name"), ["body", "evaluation", "checks", 2]),
    ((*SPEC_LOC, SECRET), ["body", "evaluation"]),  # an unknown key: dropped
    ((*SPEC_LOC, SECRET, 0), ["body", "evaluation"]),
    (("body", "name"), ["body", "name"]),
]


@pytest.mark.parametrize(("loc", "kept"), LOCATION_RULES)
def test_sanitize_error_location_rules(loc: tuple[Any, ...], kept: list[Any]) -> None:
    error = {"type": "value_error", "loc": loc, "msg": SECRET, "input": SECRET, "ctx": {SECRET: 1}}
    out = sanitize_error(error)
    assert out["loc"] == kept
    assert "input" not in out and "ctx" not in out
    if loc[:2] == ("body", "evaluation"):
        assert SECRET not in json.dumps(out)
