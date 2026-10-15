# ADR-007: Evaluation secrecy (D11)

**Status:** Accepted (Phase 4, implemented)

Related: ADR-006 (evaluation and scoring). The API projection rules are listed in [docs/api/evaluations.md](../api/evaluations.md#public-projection-rules-d11).

## Context

An evaluation spec holds **hidden** data: hidden test files, check names, expected values and answers, argv, stdin, protected paths, and evaluator-generated runner files. If any of it reaches the agent, or anyone who can feed it back to the agent, the benchmark measures memorisation, not ability.

The first Phase 4 design proposed making hidden files *readable through the API until authentication exists (Phase 8)*. That was **amended before implementation** to a strict rule:

> Hidden evaluation files, expected values and evaluator-only configuration must never be exposed through model-visible data or API responses. They are internal evaluator data, only ever mounted into the isolated evaluation container.

There is no authentication yet. So the rule has to hold **structurally**, not through permissions.

## Decision

1. **Hidden data has exactly one destination:** `/eval` in the evaluation container.
   - It is written as root, then locked: directories 0555, files 0444, root-owned.
   - The candidate's uid 1000 can read it but never write it, and `/eval/.nopyc` can't be created.
   - Hidden files are never copied into the agent's container or its snapshot, and are never offered to the agent's tools.
2. **Specs are write-only.**
   - `POST /tasks` accepts `evaluation`.
   - No endpoint ever returns it. `TaskRead` has only `has_evaluation: bool`, and is built only through `TaskRead.from_task()`; it has no `evaluation` field to serialise.
3. **`public_view()` is the single outward projection** of an evaluation.
   - `services/evaluations.py::_public()` is the only code that turns a stored evaluation into API output, and it returns `public_view(...)` and nothing else.
   - The one extension, `AttemptEvaluationRead`, adds only the attempt number and timestamps.
   - A public evaluation is made of statuses, categories, tags from a closed vocabulary (enforced: unknown tags are refused), positional check results (`position`, `passed`, `score`, `duration_ms`), score components, and trajectory metrics.
4. **Internal columns are never selected for output:** `failure_detail`, `error_message`, `integrity.tampered`, `integrity.normalized_paths`, check `name`, `expected`, `actual` and `detail`, and snapshot content. The results query selects `integrity.ok` only.
5. **No process output is persisted.** A failing hidden test's traceback quotes the hidden file, and a candidate can echo its stdin. The raw `ExecResult` lives only inside the check loop; `judge()` records exit codes, counts and timing. Expected values appear internally only as fixed templates such as `"exit 0 and all 2 expected unittest tests pass"`.
6. **Aggregates use closed vocabularies only:** statuses, failure categories, and numbers.
7. **Validation errors are sanitised (R2).**
   - **Every 422 drops `input` and `ctx`.** FastAPI's default echoes the submitted value, which would return hidden files.
   - **Errors inside `body.evaluation`** keep at most a *known* spec field name and a list index (dict keys, which are hidden file names, and unknown keys are dropped) and get the fixed message `invalid evaluation spec (<type>)`.
   - **Service-level spec errors use fixed text:** `protected_paths must name task files`, and `evaluation spec exceeds 2097152 bytes`.
   - **Spec size is checked before persistence.** The 2 MiB limit is measured the way Postgres stores `jsonb`, so a valid-per-field but oversized spec gets the fixed 422 rather than a database error.
   - **A database constraint error on task insert** maps to the fixed `task violates a database constraint`.
8. **Logs:** evaluation log events carry ids, statuses, categories and exception *types*, never check names, hidden content or process output.
   - **Exception:** an *unexpected* evaluator exception (`evaluator_error`, `evaluation_crashed`) is logged with its traceback, and that could include internal details (for example, a protected path in a `KeyError`).
   - Logs are operator-only: never API responses and never model-visible. Tightening this is noted as follow-up work.

## How it's enforced (tests, not convention)

- **Response-model snapshot:** the exact field set of every model any route can return is frozen in a test, so a new field fails until it's reviewed. A second assertion makes sure no evaluator-internal type (`CheckResult`, `IntegrityResult`, `EvaluationSpec`, the check-spec classes) is reachable from any response model.
- **Route sweep:** a marker is planted in every internal place (hidden files, check names, expected values, stored check details, `failure_detail`, `error_message`, integrity paths). Then **every** route is called, including error paths, and the marker must appear in no body or header. The test fails if a new route isn't in the sweep.
- **The 422 sanitiser** is tested on reserved and traversal file names, patterns, duplicate names, regexes, unknown keys, deep type errors, oversized values and malformed JSON.
- **E2E crawl:** on the running stack, more than 100 GETs over everything the benchmark created, including every trajectory (what the model saw). The marker never appears.
- **Model-visible data:** a runner integration test checks the FakeProvider transcript and the stored trajectory for the marker. Step 4 sandbox tests show hidden files are absent from the agent container and its snapshot.

## Residual risks (accepted, documented)

- **C7:** during grading, candidate code can *read* `/eval` and print it. Raw output is never persisted or projected, so the risk is contained to the check process. In-process cheating remains possible (ADR-006).
- **Indirect disclosure:** `PublicCheck.score` (passed ÷ expected) and the number of check positions reveal how many hidden tests and checks exist. That is the approved projection; it's not model-visible, because the agent's sandbox has no network.
- **Unexpected-exception tracebacks in worker logs** may contain internal evaluator details (operator-only; see Decision 8).
- **Authors can't read back their own spec.** A spec is write-only for everyone until an authenticated author view exists (Phase 8).
- **No authentication or rate limiting yet (Phase 8).** Anyone who can reach the API (compose binds it to `127.0.0.1`) can create datasets, tasks and batches. They still can't read hidden data back.

## Alternatives considered

- **Readable through the API until auth exists** (the original proposal). Rejected by the amendment: a leak through a public API can't be retracted later.
- **Redacting fields per response.** Rejected in favour of one projection built from scratch (allow-list, not deny-list): a new internal field is invisible by default.
