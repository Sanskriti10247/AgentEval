"""S9: Phase 4 invariants over every run the scenarios created, checked in
Postgres through the SELECT-only probe (E1) and in Docker.

Not provable black-box here: that evaluation never rewrites an attempt (it
needs a before/after snapshot; Step 5's integration tests do that). What is
checked: the record shapes, their ordering, A5, and container cleanup.
"""

import uuid

import pytest

from tests.e2e.bench import (
    NEEDS_WORKSPACE,
    Batch,
    ReadOnlyDB,
    image_id,
    leftover_containers,
)

pytestmark = pytest.mark.e2e

RECORDS = """
SELECT r.id AS run_id, r.status AS run_status, r.attempt_count,
       split_part(t.name, '-', 1) AS label,
       (SELECT count(*) FROM run_attempts x WHERE x.run_id = r.id) AS attempts,
       a.finished_at AS attempt_finished_at, a.sandbox_image_id,
       (SELECT count(*) FROM attempt_evaluations x WHERE x.attempt_id = a.id) AS evaluations,
       e.status AS eval_status, e.created_at AS eval_created_at,
       (SELECT count(*) FROM attempt_artifacts x WHERE x.attempt_id = a.id) AS artifacts,
       (SELECT count(*) FROM scores x WHERE x.attempt_evaluation_id = e.id) AS scores
FROM runs r
JOIN tasks t ON t.id = r.task_id
LEFT JOIN run_attempts a ON a.run_id = r.id AND a.attempt_no = r.attempt_count
LEFT JOIN attempt_evaluations e ON e.attempt_id = a.id
WHERE r.evaluation_id = ANY($1::uuid[])
"""


def test_s9_record_shapes_ordering_and_cleanup(
    db: ReadOnlyDB, benchmark: dict[str, Batch], failures: dict[str, Batch]
) -> None:
    batches = [*benchmark.values(), *failures.values()]
    agent_of = {row["run_id"]: b.agent for b in batches for row in b.rows}
    rows = db.select(RECORDS, [uuid.UUID(b.evaluation_id) for b in batches])
    # 36 benchmark runs (6 tasks x 6 repetitions in all) + 4 failure-scenario runs.
    assert len(rows) == len(agent_of) == 40
    sandbox = image_id()

    for r in rows:
        run = str(r["run_id"])
        agent, label = agent_of[run], r["label"]
        where = f"{agent} {label} {run}"
        # Exactly one attempt and exactly one evaluation per attempt.
        assert (r["attempt_count"], r["attempts"], r["evaluations"]) == (1, 1, 1), where
        # Evaluation is created only after the attempt is durably finished (D1/A1).
        assert r["eval_created_at"] >= r["attempt_finished_at"], where
        # A5: the image the sandbox actually ran.
        assert r["sandbox_image_id"] == sandbox, where
        # D3/A6/A2: a snapshot only for COMPLETED attempts whose spec needs the
        # workspace - and none when capture failed (the hoarder's T1).
        wants = (
            r["run_status"] == "COMPLETED"
            and label in NEEDS_WORKSPACE
            and (agent, label) != ("hoarder", "T1")
        )
        assert r["artifacts"] == int(wants), where
        # A score row exactly for graded (SUCCEEDED) evaluations.
        assert r["scores"] == int(r["eval_status"] == "SUCCEEDED"), where

    # No agent or evaluation container outlives its run.
    assert leftover_containers(set(agent_of)) == set()
