"""D11 at the API boundary (Phase 4 step 6).

1. Structural: the exact field set of every model any route can return is
   frozen here. A new field fails this test until it is reviewed. Internal
   evaluator types are unreachable from every response model.
2. Behavioural: with MARKER planted in every internal place (hidden files,
   check names, expected values, stored check details, failure_detail,
   error_message, integrity details), EVERY route is called - the test fails
   if a route exists that the sweep doesn't call - and MARKER must not appear
   in any response body or header, error responses included.
"""

import typing
import uuid
from typing import Any

import httpx
from pydantic import BaseModel

from agenteval_core.evaluation import results, spec
from agenteval_core.evaluation.scoring import ScoringConfig, ScoringWeights
from tests.integration.api_routes import api_routes
from tests.integration.eval_seed import MARKER, SM, SPEC, agent_task, create_evaluation, seed

RESPONSE_MODELS: dict[str, list[str]] = {
    "AgentRead": ["created_at", "id", "name"],
    "AgentVersionRead": [
        "agent_id", "config_hash", "created_at", "id", "limits", "model", "model_provider",
        "model_settings", "shell_allowlist", "system_prompt", "tools", "version",
    ],
    "AttemptEvaluationRead": [
        "attempt_no", "checks", "created_at", "failure_category", "finished_at", "metrics",
        "score", "status", "tags", "task_success",
    ],
    "Compare": ["dataset_id", "evaluations", "per_task", "scoring_config_hash"],
    "CompareEntry": ["agent_version_id", "evaluation_id", "summary"],
    "CostTotals": ["mean_per_attempt", "total"],
    "DatasetRead": [
        "created_at", "description", "frozen_at", "id", "name", "status", "task_count",
        "task_ids", "version",
    ],
    "EvaluationRead": [
        "agent_version_id", "cancel_requested_at", "created_at", "dataset_id",
        "eval_max_attempts", "id", "name", "progress", "repetitions", "run_max_attempts",
        "scoring_config", "scoring_config_hash",
    ],
    "Latency": ["p50", "p95"],
    "MeanScore": ["efficiency", "overall", "reliability", "task_success", "test_score"],
    "Progress": [
        "awaiting_evaluation", "complete", "evaluations_by_status", "runs_by_status",
        "runs_total",
    ],
    "PublicCheck": ["duration_ms", "passed", "position", "score"],
    "PublicEvaluation": [
        "checks", "failure_category", "metrics", "score", "status", "tags", "task_success",
    ],
    "PublicScore": [
        "efficiency", "integrity_gate_applied", "overall", "reliability", "task_success",
        "test_score",
    ],
    "ResultPage": ["next_after", "results"],
    "ResultRow": [
        "attempt_no", "cost_usd", "duration_ms", "evaluation", "repetition", "run_id",
        "run_outcome", "run_status", "steps", "task_id", "tokens_in", "tokens_out",
    ],
    "RunAttemptRead": [
        "attempt_no", "cost_usd", "duration_ms", "error_message", "exit_code", "final_answer",
        "finished_at", "id", "oom_killed", "outcome", "run_id", "sandbox_image_id",
        "started_at", "status", "stderr", "stderr_truncated", "stdout", "stdout_truncated",
        "steps", "tokens_in", "tokens_out", "worker_id",
    ],
    "RunPage": ["next_after", "runs"],
    "RunRead": [
        "agent_version_id", "attempt_count", "cancel_reason", "cancel_requested_at", "claimed_at",
        "created_at", "dead_letter_reason", "error_message", "evaluation_id", "failure_reason",
        "finished_at", "id", "idempotency_key", "max_attempts", "next_attempt_at", "outcome",
        "repetition", "replay_count", "started_at", "status", "task_id", "worker_id",
    ],
    "ScoringConfig": ["version", "weights"],
    "ScoringWeights": ["efficiency", "reliability", "task_success", "test_score"],
    "Summary": [
        "attempts", "ci95", "complete", "cost_usd", "evaluation_errors", "evaluation_id",
        "failure_categories", "graded", "in_progress", "latency_ms", "mean_score", "passed",
        "per_task", "platform_failure_rate", "platform_failures", "success_rate", "tokens",
        "ungraded",
    ],
    "TaskComparison": ["delta_vs_first", "success_rate", "task_id"],
    "TaskRead": [
        "command", "cpu_limit", "created_at", "dataset_id", "description", "difficulty",
        "files", "has_evaluation", "id", "image", "instructions", "memory_mb", "name",
        "task_type", "test_command", "timeout_s",
    ],
    "TaskSummary": [
        "attempts", "difficulty", "graded", "mean_overall", "passed", "success_rate",
        "task_id", "task_name",
    ],
    "TokenTotals": ["tokens_in", "tokens_out"],
    "TrajectoryEventRead": [
        "cache_read_tokens", "cache_write_tokens", "cost_usd", "duration_ms", "error_class",
        "event_type", "input", "output", "seq", "status", "step", "tokens_in", "tokens_out",
        "tool", "ts",
    ],
    "TrajectoryMetrics": [
        "agent_tool_errors", "last_tests_passed", "limits_reached", "loop_detected",
        "max_identical_streak", "model_calls", "model_errors", "ran_tests", "tool_calls",
        "tool_error_rate", "tool_status_counts",
    ],
    "TrajectoryPage": ["events", "next_after_seq"],
}  # fmt: skip

# Evaluator-internal types (D11): never reachable from a response model.
INTERNAL = {
    results.CheckResult, results.IntegrityResult, spec.EvaluationSpec, spec.PythonTestsCheck,
    spec.IoCheck, spec.CommandCheck, spec.FinalAnswerCheck, spec.FileCheck,
}  # fmt: skip


def reachable_models(app: Any) -> dict[type[BaseModel], None]:
    seen: dict[type[BaseModel], None] = {}

    def walk(tp: Any) -> None:
        if typing.get_origin(tp) is not None:
            for arg in typing.get_args(tp):
                walk(arg)
        elif isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in seen:
            seen[tp] = None
            for field in tp.model_fields.values():
                walk(field.annotation)

    for _, _, route in api_routes(app):
        walk(route.response_model)
    return seen


def test_response_models_are_exactly_the_reviewed_public_shapes(client: httpx.AsyncClient) -> None:
    models = reachable_models(client.app)  # type: ignore[attr-defined]
    assert {m.__name__: sorted(m.model_fields) for m in models} == RESPONSE_MODELS
    assert not INTERNAL & set(models)


# -- the sweep -----------------------------------------------------------------------------------


async def test_no_route_returns_hidden_evaluation_data(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)
    ev = s.evaluation_id
    a1, b2 = s.runs["A1"], s.runs["B2"]
    other = (await create_evaluation(client, s.dataset_id, s.version_id, repetitions=1)).json()
    mismatched = await create_evaluation(
        client, s.dataset_id, s.version_id,
        scoring_config=ScoringConfig(weights=ScoringWeights(
            task_success=1, test_score=0, reliability=0, efficiency=0)),
    )  # fmt: skip
    agent = (await client.post("/api/v1/agents", json={"name": f"a-{uuid.uuid4().hex[:6]}"})).json()
    version_body = {
        "model_provider": "fake", "model": "fake-model", "system_prompt": "x",
        "tools": ["read_file"], "model_settings": {"script": [{"type": "turn", "text": "hi"}]},
    }  # fmt: skip
    invalid_spec = agent_task(evaluation={**SPEC, "hidden_files": {"conftest.py": MARKER}})

    calls: list[tuple[str, str, str, dict[str, Any]]] = [
        ("GET", "/health", "/health", {}),
        ("GET", "/ready", "/ready", {}),
        ("POST", "/api/v1/agents", "/api/v1/agents",
         {"json": {"name": f"b-{uuid.uuid4().hex[:6]}"}}),
        ("GET", "/api/v1/agents/{agent_id}", f"/api/v1/agents/{agent['id']}", {}),
        ("POST", "/api/v1/agents/{agent_id}/versions",
         f"/api/v1/agents/{agent['id']}/versions", {"json": version_body}),
        ("GET", "/api/v1/agents/{agent_id}/versions", f"/api/v1/agents/{agent['id']}/versions", {}),
        ("GET", "/api/v1/agents/{agent_id}/versions/{number}",
         f"/api/v1/agents/{agent['id']}/versions/1", {}),
        ("POST", "/api/v1/tasks", "/api/v1/tasks", {"json": agent_task(evaluation=SPEC)}),
        ("POST", "/api/v1/tasks", "/api/v1/tasks", {"json": invalid_spec}),
        ("GET", "/api/v1/tasks/{task_id}", f"/api/v1/tasks/{s.tasks['A']}", {}),
        ("POST", "/api/v1/runs", "/api/v1/runs",
         {"json": {"task_id": s.tasks["A"], "agent_version_id": str(s.version_id)}}),
        ("GET", "/api/v1/runs/{run_id}", f"/api/v1/runs/{b2}", {}),
        ("GET", "/api/v1/runs/{run_id}/attempts", f"/api/v1/runs/{b2}/attempts", {}),
        ("GET", "/api/v1/runs/{run_id}/attempts/{attempt_no}/events",
         f"/api/v1/runs/{a1}/attempts/1/events", {}),
        *[
            ("GET", "/api/v1/runs/{run_id}/attempts/{attempt_no}/evaluation",
             f"/api/v1/runs/{run}/attempts/1/evaluation", {})
            for run in s.runs.values()
        ],
        ("POST", "/api/v1/datasets", "/api/v1/datasets", {"json": {"name": "x", "version": "1"}}),
        ("GET", "/api/v1/datasets/{dataset_id}", f"/api/v1/datasets/{s.dataset_id}", {}),
        ("POST", "/api/v1/datasets/{dataset_id}/freeze",
         f"/api/v1/datasets/{s.dataset_id}/freeze", {}),
        ("POST", "/api/v1/evaluations", "/api/v1/evaluations",
         {"json": {"name": "n", "dataset_id": s.dataset_id,
                   "agent_version_id": str(s.version_id)}}),
        ("GET", "/api/v1/evaluations/compare", "/api/v1/evaluations/compare",
         {"params": {"ids": f"{ev},{other['id']}"}}),
        ("GET", "/api/v1/evaluations/compare", "/api/v1/evaluations/compare",
         {"params": {"ids": f"{ev},{mismatched.json()['id']}"}}),
        ("GET", "/api/v1/evaluations/{evaluation_id}", f"/api/v1/evaluations/{ev}", {}),
        ("GET", "/api/v1/evaluations/{evaluation_id}/results",
         f"/api/v1/evaluations/{ev}/results", {"params": {"limit": 500}}),
        ("GET", "/api/v1/evaluations/{evaluation_id}/summary",
         f"/api/v1/evaluations/{ev}/summary", {}),
        # Cancels last: they change run states the GETs above read (C3 is still QUEUED, B2 is
        # COMPLETED -> 409).
        # The seed's FAILED runs carry MARKER in their (public) run error_message, so the
        # sweep lists COMPLETED ones; replaying B3 below clears that message.
        ("GET", "/api/v1/runs", "/api/v1/runs", {"params": {"status": "COMPLETED", "limit": 200}}),
        ("GET", "/api/v1/runs", "/api/v1/runs", {"params": {"dead_letter": "true"}}),
        ("GET", "/api/v1/runs", "/api/v1/runs", {"params": {"after": "garbage"}}),
        ("POST", "/api/v1/runs/{run_id}/replay", f"/api/v1/runs/{s.runs['B3']}/replay", {}),
        ("POST", "/api/v1/runs/{run_id}/replay", f"/api/v1/runs/{b2}/replay", {}),
        ("POST", "/api/v1/runs/{run_id}/cancel", f"/api/v1/runs/{s.runs['C3']}/cancel", {}),
        ("POST", "/api/v1/runs/{run_id}/cancel", f"/api/v1/runs/{b2}/cancel", {}),
        ("POST", "/api/v1/evaluations/{evaluation_id}/cancel",
         f"/api/v1/evaluations/{ev}/cancel", {}),
    ]  # fmt: skip

    statuses = {}
    for method, template, url, kw in calls:
        resp = await client.request(method, url, **kw)
        assert resp.status_code < 500, (url, resp.text)
        assert MARKER not in resp.text, (url, resp.status_code)
        assert MARKER not in str(resp.headers), url
        statuses.setdefault(template, set()).add(resp.status_code)

    every_route = {(m, p) for m, p, _ in api_routes(client.app)}  # type: ignore[attr-defined]
    assert {(m, t) for m, t, _, _ in calls} == every_route, "a route is missing from the sweep"
    # The evaluation-bearing calls really returned data, and errors were exercised.
    assert statuses["/api/v1/evaluations/{evaluation_id}/results"] == {200}
    assert {200, 404} <= statuses["/api/v1/runs/{run_id}/attempts/{attempt_no}/evaluation"]
    assert {200, 422} <= statuses["/api/v1/evaluations/compare"]
    assert {201, 422} <= statuses["/api/v1/tasks"]
    assert {200, 422} <= statuses["/api/v1/runs"]
    assert {202, 409} <= statuses["/api/v1/runs/{run_id}/replay"]


# -- the snapshot: what live responses actually contain -------------------------------------

# Names that exist on internal rows and must never be a key anywhere in a public body (D11, F15).
INTERNAL_KEYS = frozenset({
    "lease_epoch", "lease_expires_at", "heartbeat_at", "dispatched_at", "unstarted_claims",
    "storage_key", "workspace_capture", "error_reason", "error_kind", "eval_attempts",
    "max_eval_attempts", "failure_detail", "integrity", "hidden_files", "retryable",
    "content", "sha256",
    # evaluation check internals: only position/passed/score/duration_ms are public
    "name", "kind", "expected", "actual", "detail", "required", "weight", "reliability_detail",
})  # fmt: skip


def all_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in all_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in all_keys(v)}
    return set()


async def test_public_json_key_sets_are_pinned_and_no_internal_field_leaks(
    client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    s = await seed(client, sessionmaker)
    a1, b3 = s.runs["A1"], s.runs["B3"]

    async def get(url: str, **params: Any) -> Any:
        resp = await client.get(url, params=params)
        assert resp.status_code == 200, (url, resp.text)
        return resp.json()

    run = await get(f"/api/v1/runs/{a1}")
    attempts = await get(f"/api/v1/runs/{a1}/attempts")
    listing = await get("/api/v1/runs", status="COMPLETED", limit=5)
    evaluation = await get(f"/api/v1/evaluations/{s.evaluation_id}")
    results = await get(f"/api/v1/evaluations/{s.evaluation_id}/results", limit=500)
    summary = await get(f"/api/v1/evaluations/{s.evaluation_id}/summary")
    attempt_evaluation = await get(f"/api/v1/runs/{a1}/attempts/1/evaluation")
    failed_without_attempt = await get(f"/api/v1/runs/{b3}")

    run_keys = set(RESPONSE_MODELS["RunRead"])
    assert set(run) == set(failed_without_attempt) == run_keys
    assert set(listing) == {"runs", "next_after"} and listing["runs"]
    assert all(set(r) == run_keys for r in listing["runs"])
    assert set(attempts[0]) == set(RESPONSE_MODELS["RunAttemptRead"])
    assert set(evaluation) == set(RESPONSE_MODELS["EvaluationRead"])
    assert set(evaluation["progress"]) == set(RESPONSE_MODELS["Progress"])
    assert set(evaluation["progress"]["runs_by_status"]) == {
        "QUEUED", "CLAIMED", "RUNNING", "RETRYING", "COMPLETED", "FAILED", "CANCELLED",
    }  # fmt: skip
    assert set(results) == {"results", "next_after"}
    assert all(set(row) == set(RESPONSE_MODELS["ResultRow"]) for row in results["results"])
    graded = [r["evaluation"] for r in results["results"] if r["evaluation"] is not None]
    assert graded and all(set(e) == set(RESPONSE_MODELS["PublicEvaluation"]) for e in graded)
    assert {k for e in graded for c in e["checks"] for k in c} == set(
        RESPONSE_MODELS["PublicCheck"]
    )
    assert set(summary) == set(RESPONSE_MODELS["Summary"])
    assert set(attempt_evaluation) == set(RESPONSE_MODELS["AttemptEvaluationRead"])
    assert set(attempt_evaluation["checks"][0]) == set(RESPONSE_MODELS["PublicCheck"])

    for name, body in {
        "run": run, "attempts": attempts, "listing": listing, "evaluation": evaluation,
        "results": results, "summary": summary, "attempt evaluation": attempt_evaluation,
    }.items():  # fmt: skip
        # ScoringConfig/weights/`task_name` are public; "name" is only banned as a check key.
        leaked = all_keys(body) & (INTERNAL_KEYS - {"name"})
        assert not leaked, (name, leaked)
        assert MARKER not in str(body), name
    for e in [*graded, attempt_evaluation]:
        assert all_keys(e["checks"]).isdisjoint(INTERNAL_KEYS)
