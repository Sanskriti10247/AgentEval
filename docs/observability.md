# Observability

Metrics, traces and logs for the API and the workers. The design and its alternatives are in [ADR-010](decisions/ADR-010-observability.md). Instrumentation is best-effort and adds no database round trips: an exporter or scrape failure never fails a run.

## Metrics

Both processes export the Prometheus text format (ADR-010 A).

```bash
docker compose --profile observability up -d prometheus   # UI: http://127.0.0.1:9090
curl -s localhost:8000/metrics | head                      # the API, directly
```

The API serves `GET /metrics`. HTTP requests and latency are labelled by route template (`/api/v1/runs/{run_id}`, never the raw path; unmatched paths share one `unmatched` label), and `/metrics` is not counted in its own numbers. The system-state gauges are read from Postgres and Redis on every scrape. If one source is down, `agenteval_scrape_errors_total{source}` goes up, that source's gauges disappear, and the scrape still returns 200.

Each worker serves its own endpoint on `AGENTEVAL_WORKER_METRICS_PORT` (default 9464, `0` disables it) from a thread. In compose that port is exposed to the compose network only, for Prometheus, and never published to the host. The Prometheus UI is bound to `127.0.0.1`. `/metrics` and the worker endpoints are unauthenticated by design and meant for the internal network only.

Labels are closed sets (status, outcome, failure reason and category, transition code, tool name, provider and model, stream), never ids. A tool name the model invented is recorded as `unknown`, and an unpriced model as `other`.

| Family | Type | Labels |
|---|---|---|
| `agenteval_http_requests_total`, `agenteval_http_request_duration_seconds` | counter, histogram | `method`, `route`, `status` (no `status` on the histogram) |
| `agenteval_runs_by_status`, `agenteval_evaluations_by_status` | gauge | `status` (evaluations are the per-attempt grading rows) |
| `agenteval_backlog`, `agenteval_backlog_oldest_age_seconds` | gauge | `kind`: `queued_run`, `retrying_run`, `pending_evaluation` |
| `agenteval_workers_live` | gauge | workers not stopped and heartbeating within 3 lease TTLs |
| `agenteval_stream_length`, `agenteval_stream_pending` | gauge | `stream` |
| `agenteval_scrape_errors_total` | counter | `source`: `postgres`, `redis` |
| `agenteval_auth_failures_total` | counter | `reason`: `missing`, `invalid`, `revoked`, `expired` (401), `forbidden` (403) |
| `agenteval_rate_limited_total`, `agenteval_rate_limit_errors_total` | counter | `kind` (`read`, `write`) for 429s; none for Redis failures (the request was allowed, fail open) |
| `agenteval_runs_finished_total` | counter | `status` (terminal), `outcome` (`none` when cancelled) |
| `agenteval_run_transitions_total` | counter | `code` (ADR-009: R1, R3, R7, R8 ...), `actor` (`worker`, `reconciler`). Retries are R8 and R9; recoveries are the reconciler's R4a, R4b, R5, R9, R10, R11, R12 |
| `agenteval_attempt_failures_total` | counter | `failure_reason`, `category` |
| `agenteval_run_dead_letters_total`, `agenteval_run_cancellations_total` | counter | `reason` |
| `agenteval_run_queue_wait_seconds` | histogram | creation to first claim (a retry's backoff is not queueing) |
| `agenteval_run_duration_seconds` | histogram | `outcome`: one finished attempt |
| `agenteval_evaluation_transitions_total`, `agenteval_evaluation_duration_seconds` | counter, histogram | `code`; `status` |
| `agenteval_model_call_duration_seconds`, `agenteval_model_tokens_total`, `agenteval_model_cost_usd_total`, `agenteval_model_call_errors_total` | histogram, counters | `provider`, `model`; `direction` (`input`, `output`); `kind` |
| `agenteval_tool_calls_total`, `agenteval_tool_call_duration_seconds` | counter, histogram | `tool`, `status` |
| `agenteval_lease_losses_total`, `agenteval_artifact_saves_total`, `agenteval_reconciler_step_errors_total` | counters | `kind`; `result`; `step` |
| `agenteval_worker_inflight`, `agenteval_worker_concurrency` | gauge | `kind` (`run`, which includes its inline grading, and `evaluation`); the configured slots |

Some useful queries:

```promql
# Run success rate over 15 minutes: completed / (completed + failed); cancelled runs are excluded
sum(rate(agenteval_runs_finished_total{status="COMPLETED"}[15m]))
  / sum(rate(agenteval_runs_finished_total{status=~"COMPLETED|FAILED"}[15m]))

# p95 attempt duration, by outcome
histogram_quantile(0.95, sum by (le, outcome) (rate(agenteval_run_duration_seconds_bucket[15m])))

# Backlog now, and how long the oldest queued run has waited
agenteval_backlog{kind="queued_run"}
agenteval_backlog_oldest_age_seconds{kind="queued_run"}

# Retries per minute, and dead letters by reason
sum(rate(agenteval_run_transitions_total{code=~"R8|R9"}[5m])) * 60
sum by (reason) (increase(agenteval_run_dead_letters_total[1h]))

# Spend per hour by model, and worker saturation
sum by (model) (increase(agenteval_model_cost_usd_total[1h]))
sum(agenteval_worker_inflight{kind="run"}) / sum(agenteval_worker_concurrency)
```

A few things to keep in mind when reading them. Ages are measured from creation for a queued run (a run requeued after a retry or a recovery keeps its original `created_at`), and from the moment it became due for a retrying run or a pending evaluation. Counters reset when a worker restarts, so use `rate` or `increase`. A run recovered after a crash is counted by whichever process applied each transition, so sums across workers are the system totals.

## Tracing

Traces use OpenTelemetry (ADR-010 B). Spans are always created, since that is cheap and in-process, so `trace_id` and `span_id` appear on every log line written inside one, and the trace context travels through Redis. Nothing leaves the process unless `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set; `OTEL_TRACES_EXPORTER=none` forces export off. The SDK honours the standard `OTEL_*` variables (service name, sampler, batch tuning, headers). The services are named `agenteval-api` and `agenteval-worker`.

```bash
make observability-up     # Prometheus + Jaeger; api and worker export traces to Jaeger
# Jaeger UI: http://127.0.0.1:16686   (service: agenteval-api or agenteval-worker)
make observability-down   # removes both, recreates api and worker without export
```

`make observability-up` applies `docker-compose.observability.yml` on top of the base file and starts the `observability` profile. That override only sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` on `api` and `worker`. Using an override file, rather than a variable that defaults to empty in the base file, keeps the default stack free of any `OTEL_*` setting: it exports nothing by construction, and `make up` and `make e2e` never depend on a collector. Jaeger keeps traces in memory, so they are gone on restart. Its OTLP port is internal to the compose network, and its UI is bound to `127.0.0.1` and unauthenticated.

A run submitted through the API is one trace across the Redis hop; the W3C `traceparent` travels as a field of the stream message:

```text
POST /api/v1/runs                      (server span; named by the route template)
└─ queue.publish                       (producer)
   └─ run.execute                      (consumer, in the worker)
      ├─ run.claim
      ├─ attempt
      │  ├─ agent.step                 (one per model turn)
      │  │  ├─ model.call              (one per provider call, retries included)
      │  │  └─ tool.call               (one per tool call)
      │  ├─ artifact.save
      │  └─ run.finish
      └─ evaluation.grade
         └─ evaluation.check
```

Every reconciler step is its own root span, `reconciler.<step>` (recoveries, retries due, redispatch, sweeps). Its `queue.publish` is a child, so a redispatched run's next `run.execute` hangs off that reconciler trace. Steps that found nothing to do are marked `idle` and not exported, since there are several per second. A grading delivered on the evaluations stream after a recovery is an `evaluation.grade` consumer span, joined to its publisher the same way. A message with no `traceparent` (published before tracing was configured, or by an older publisher) makes `run.execute` a new root with `trace_origin=new_root`; its `run_id` attribute links it to everything else about the run.

### What is never recorded

Span attributes come from an allow-list in `agenteval_core/observability/tracing.py`: ids (`run_id`, `attempt_no`, `evaluation_id`, `worker_id`), enums (status, outcome, failure reason, transition code, tool name, provider, model), and sizes, durations, token counts and cost. A key outside the list is dropped, so there is nowhere to put prompts, model output, tool inputs or outputs, file contents, hidden checks or error messages from candidate code (D11, [ADR-007](decisions/ADR-007-evaluation-secrecy.md)). A tool name the model invented is recorded as `unknown`, and an unpriced model is omitted. An exception is recorded as its type name only, never its message or traceback. An exporter failure costs a warning in the log, never a run.

## Logs

Logs are structured JSON on stdout, with `run_id`, `task_id`, `attempt_no` and `worker_id` bound per job, and `trace_id`/`span_id` inside a span. Secrets are redacted before a line is written (see the [security review](security-review.md)). Beyond metrics, traces and logs, the trajectory and the run, attempt, evaluation and `workers` rows in Postgres are the record of what happened.

For production (SSH tunnels to the UIs, log rotation, retention) see [deployment.md](deployment.md#observability-in-production).
