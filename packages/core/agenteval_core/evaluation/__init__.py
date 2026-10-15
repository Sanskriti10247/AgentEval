"""Phase 4 evaluation domain: deterministic grading, scoring and failure
classification. Pure - no Docker, sandbox, worker or API code. The Step 5
runner executes ExecPlans in the isolated evaluation container and feeds the
results back through these functions.

Bump EVALUATOR_VERSION whenever grading semantics change; it is stored on
every attempt evaluation (and the scoring-config hash on every score) so a
result can be traced to exactly how it was produced. The spec itself lives on
the immutable task (EvaluationSpec.spec_hash() identifies it).
"""

EVALUATOR_VERSION = "phase4.1"
