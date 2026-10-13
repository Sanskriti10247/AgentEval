"""Phase 4 benchmark scenarios against the running stack (S1-S4, S8).

Dataset -> tasks -> freeze -> evaluation -> dispatch -> worker -> agent ->
sandbox -> snapshot -> evaluation container -> scoring -> persistence ->
results -> summary -> compare, all over real HTTP. FakeProvider only.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

import httpx
import pytest

from agenteval_core.evaluation.scoring import DEFAULT_SCORING_CONFIG, ScoringConfig, q
from agenteval_core.evaluation.stats import wilson_interval
from tests.e2e.bench import (
    API,
    BENCH_TASKS,
    COST_PER_CALL,
    EXPECTED,
    MARKER,
    ONLY_SUCCESS,
    SPECS,
    STEPS,
    Batch,
    all_events,
    ok,
)

pytestmark = pytest.mark.e2e
GRADED_AGENTS = ["fixer", "quitter", "cheater", "looper"]


def expected_cells(batch: Batch) -> list[tuple[str, int, str | None]]:
    return [
        (label, rep, EXPECTED[batch.agent][label])
        for label in BENCH_TASKS
        for rep in range(1, batch.repetitions + 1)
    ]


# -- S1: every cell of the benchmark ---------------------------------------------------------


@pytest.mark.parametrize("agent", GRADED_AGENTS)
def test_s1_every_run_is_executed_graded_and_persisted(
    benchmark: dict[str, Batch], agent: str
) -> None:
    batch = benchmark[agent]
    cells = batch.by_label()
    assert len(batch.rows) == len(BENCH_TASKS) * batch.repetitions
    for label, rep, category in expected_cells(batch):
        row = cells[(label, rep)]
        where = f"{agent} {label} rep {rep}"
        outcome = "MAX_STEPS" if agent == "looper" else "AGENT_FINISHED"
        assert (row["run_status"], row["run_outcome"], row["attempt_no"]) == (
            "COMPLETED", outcome, 1,
        ), where  # fmt: skip
        steps = STEPS[agent]
        assert row["steps"] == steps, where
        assert Decimal(row["cost_usd"]) == COST_PER_CALL * steps, where
        assert (row["tokens_in"], row["tokens_out"]) == (100 * steps, 20 * steps), where

        ev = row["evaluation"]
        assert ev is not None, where
        assert ev["failure_category"] == category, where
        if SPECS[label] is None:  # R7: ungraded
            assert (ev["status"], ev["task_success"], ev["score"]) == ("SKIPPED", None, None)
            continue
        assert ev["status"] == "SUCCEEDED", where
        assert ev["task_success"] is (category == "passed"), where
        assert len(ev["checks"]) == len(SPECS[label]["checks"]), where  # type: ignore[index]
        assert [c["position"] for c in ev["checks"]] == list(range(1, len(ev["checks"]) + 1))
        if category == "passed":
            assert all(c["passed"] for c in ev["checks"]), where
        assert ev["score"] is not None, where
        assert Decimal(ev["score"]["task_success"]) == (1 if category == "passed" else 0)


def test_s1_tampering_is_caught_while_the_hidden_tests_still_run(
    benchmark: dict[str, Batch],
) -> None:
    ev = benchmark["cheater"].by_label()[("T1", 1)]["evaluation"]
    assert ev["failure_category"] == "tampering"
    assert ev["checks"][0]["passed"] is True  # the fix itself is correct
    assert ev["score"]["integrity_gate_applied"] is True
    assert Decimal(ev["score"]["overall"]) == 0


# -- S2: summary statistics (D7, R7) -----------------------------------------------------------


@pytest.mark.parametrize("agent", GRADED_AGENTS)
def test_s2_summary_statistics_are_exact(
    stack: httpx.Client, benchmark: dict[str, Batch], agent: str
) -> None:
    batch = benchmark[agent]
    summary = ok(stack.get(f"{API}/evaluations/{batch.evaluation_id}/summary"), 200)
    cells = expected_cells(batch)
    graded = sum(SPECS[label] is not None for label, _, _ in cells)
    passed = sum(cat == "passed" for _, _, cat in cells)
    attempts = len(cells)

    assert summary["complete"] is True and summary["in_progress"] == 0
    assert (summary["attempts"], summary["graded"], summary["passed"]) == (attempts, graded, passed)
    assert summary["ungraded"] == attempts - graded
    assert (summary["platform_failures"], summary["evaluation_errors"]) == (0, 0)
    assert summary["platform_failure_rate"] == 0
    assert summary["success_rate"] == pytest.approx(passed / graded)
    assert summary["ci95"] == pytest.approx(list(wilson_interval(passed, graded) or ()))
    assert summary["failure_categories"] == dict(Counter(c for _, _, c in cells if c is not None))
    steps = STEPS[agent] * attempts
    assert Decimal(summary["cost_usd"]["total"]) == COST_PER_CALL * steps
    assert summary["tokens"] == {"tokens_in": 100 * steps, "tokens_out": 20 * steps}
    latency = summary["latency_ms"]
    assert 0 < latency["p50"] <= latency["p95"]
    per_task = {t["task_id"]: t for t in summary["per_task"]}
    for label in BENCH_TASKS:
        row = per_task[batch.tasks[label]]
        task_passed = sum(c == "passed" for lb, _, c in cells if lb == label)
        assert row["attempts"] == batch.repetitions
        if SPECS[label] is None:
            assert (row["graded"], row["success_rate"]) == (0, None)
        else:
            assert row["success_rate"] == pytest.approx(task_passed / batch.repetitions)


# -- S3: repetition determinism and scoring --------------------------------------------------------


def _stable(ev: dict[str, Any]) -> dict[str, Any]:
    return {**ev, "checks": [{k: v for k, v in c.items() if k != "duration_ms"}
                             for c in ev["checks"]]}  # fmt: skip


def test_s3_repetitions_are_deterministic(benchmark: dict[str, Batch]) -> None:
    cells = benchmark["fixer"].by_label()
    for label in BENCH_TASKS:
        assert _stable(cells[(label, 1)]["evaluation"]) == _stable(cells[(label, 2)]["evaluation"])


@pytest.mark.parametrize("agent", [*GRADED_AGENTS, "fixer-weights"])
def test_s3_overall_is_the_weighted_sum_of_stored_components(
    stack: httpx.Client, benchmark: dict[str, Batch], agent: str
) -> None:
    batch = benchmark[agent]
    detail = ok(stack.get(f"{API}/evaluations/{batch.evaluation_id}"), 200)
    config = ScoringConfig.model_validate(detail["scoring_config"])
    assert detail["scoring_config_hash"] == config.config_hash()
    w = config.weights
    for row in batch.rows:
        score = row["evaluation"]["score"]
        if score is None:
            continue
        c = {k: Decimal(v) if v is not None else Decimal(0) for k, v in score.items()
             if k not in ("overall", "integrity_gate_applied")}  # fmt: skip
        expected = q(
            w.task_success * c["task_success"] + w.test_score * c["test_score"]
            + w.reliability * c["reliability"] + w.efficiency * c["efficiency"]
        )  # fmt: skip
        if score["integrity_gate_applied"]:
            expected = Decimal(0)
        assert Decimal(score["overall"]) == expected, row["run_id"]


def test_s3_a_batch_scores_under_its_own_weights(
    stack: httpx.Client, benchmark: dict[str, Batch]
) -> None:
    custom = ScoringConfig.model_validate(ONLY_SUCCESS)
    detail = ok(stack.get(f"{API}/evaluations/{benchmark['fixer-weights'].evaluation_id}"), 200)
    assert detail["scoring_config_hash"] == custom.config_hash()
    assert custom.config_hash() != DEFAULT_SCORING_CONFIG.config_hash()
    for row in benchmark["fixer-weights"].rows:
        score = row["evaluation"]["score"]
        if score is not None:
            assert Decimal(score["overall"]) == Decimal(score["task_success"])
    summary = ok(
        stack.get(f"{API}/evaluations/{benchmark['fixer-weights'].evaluation_id}/summary"), 200
    )
    assert Decimal(summary["mean_score"]["overall"]) == Decimal("0.8")  # 4 of 5 graded pass


# -- S4: comparison --------------------------------------------------------------------------------


def test_s4_compare_pairs_every_task_across_agents(
    stack: httpx.Client, benchmark: dict[str, Batch]
) -> None:
    ids = [benchmark[a].evaluation_id for a in GRADED_AGENTS]
    body = ok(stack.get(f"{API}/evaluations/compare", params={"ids": ",".join(ids)}), 200)
    assert [e["evaluation_id"] for e in body["evaluations"]] == ids
    tasks = benchmark["fixer"].tasks
    by_task = {t["task_id"]: t for t in body["per_task"]}
    for label in BENCH_TASKS:
        row = by_task[tasks[label]]
        for agent, ev_id in zip(GRADED_AGENTS, ids, strict=True):
            if SPECS[label] is None:
                assert row["success_rate"][ev_id] is None
                continue
            rate = 1.0 if EXPECTED[agent][label] == "passed" else 0.0
            assert row["success_rate"][ev_id] == rate, (agent, label)
            base = 1.0 if EXPECTED["fixer"][label] == "passed" else 0.0
            assert row["delta_vs_first"][ev_id] == pytest.approx(rate - base), (agent, label)


def test_s4_compare_refuses_unlike_evaluations(
    stack: httpx.Client, benchmark: dict[str, Batch], failures: dict[str, Batch]
) -> None:
    fixer = benchmark["fixer"].evaluation_id
    for other in (benchmark["fixer-weights"], failures["hoarder"]):  # weights / dataset differ
        resp = stack.get(
            f"{API}/evaluations/compare", params={"ids": f"{fixer},{other.evaluation_id}"}
        )
        assert resp.status_code == 422, resp.text


# -- S8: D11 across the live stack -----------------------------------------------------------------


def test_s8_no_endpoint_reveals_hidden_evaluation_data(
    stack: httpx.Client, benchmark: dict[str, Batch], failures: dict[str, Batch]
) -> None:
    """Crawl every GET for everything the benchmark created - including the
    trajectory, i.e. everything the model saw."""
    seen = 0

    def get(url: str, **kw: Any) -> None:
        nonlocal seen
        resp = stack.get(url, **kw)
        assert resp.status_code in (200, 404), (url, resp.text)
        assert MARKER not in resp.text, url
        seen += 1

    batches = [*benchmark.values(), *failures.values()]
    datasets = set()
    for b in batches:
        detail = ok(stack.get(f"{API}/evaluations/{b.evaluation_id}"), 200)
        datasets.add(detail["dataset_id"])
        get(f"{API}/evaluations/{b.evaluation_id}")
        get(f"{API}/evaluations/{b.evaluation_id}/summary")
        get(f"{API}/evaluations/{b.evaluation_id}/results", params={"limit": 3})
        for row in b.rows:
            run = row["run_id"]
            get(f"{API}/runs/{run}")
            get(f"{API}/runs/{run}/attempts")
            get(f"{API}/runs/{run}/attempts/1/evaluation")
            events = all_events(stack, run)
            assert MARKER not in str(events), run  # model-visible: never saw hidden data
    for ds in datasets:
        get(f"{API}/datasets/{ds}")
    for task_id in {t for b in batches for t in b.tasks.values()}:
        get(f"{API}/tasks/{task_id}")
    ids = ",".join(benchmark[a].evaluation_id for a in GRADED_AGENTS)
    get(f"{API}/evaluations/compare", params={"ids": ids})
    assert seen > 100
