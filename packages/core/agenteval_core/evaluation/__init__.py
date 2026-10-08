"""Phase 4 evaluation domain: deterministic grading, scoring and failure
classification. Pure - no Docker, sandbox, worker or API code. The Step 5
runner executes ExecPlans in the isolated evaluation container and feeds the
results back through these functions.

Bump EVALUATOR_VERSION whenever grading semantics change; it is stored on
every attempt evaluation (with the spec and scoring-config hashes) so a
result can be traced to exactly how it was produced.
"""

EVALUATOR_VERSION = "phase4.1"
