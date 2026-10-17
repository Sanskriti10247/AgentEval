# ADR-010: Observability — Prometheus metrics, OpenTelemetry traces, and a web dashboard

**Status:** Accepted (Phase 6).

Related: ADR-004 (modular monolith + workers), ADR-009 (reliability and recovery).

## Context

Phases 1–5 made AgentEval correct under failure, but the only window into a running system is structured JSON logs (`run_id`, `task_id`, `attempt_no`, `worker_id` bound per job) and the database. That answers "what happened to run X" but not:

- Is the queue growing? How many workers are alive and how busy are they?
- How long do runs and gradings take, at p50/p95? What do model calls cost?
- How often do attempts fail, why, and how many are retried, recovered or dead-lettered?
- For one slow run: where did the time go — queue wait, sandbox start, model calls, tools, grading?
- How do agents compare, without writing SQL?

Phase 6 adds the three standard signals — metrics, traces, a dashboard — and must not change any Phase 5 behaviour or add latency to the paths ADR-008 measured (heartbeats, cancellation, T-finish).

## Decision

### A. Metrics: `prometheus-client`, pull model

- **Why Prometheus:** the de-facto exposition format, a small pure-Python client, no agent or sidecar, and every tool (Prometheus, Grafana, managed backends) reads it. A push gateway or StatsD would add a moving part for no gain at this scale.
- **API** exposes `GET /metrics`:
  - `agenteval_http_requests_total{method, route, status}` and `agenteval_http_request_duration_seconds{method, route}` (route = the template, e.g. `/runs/{run_id}`, never a raw path, to keep cardinality bounded);
  - scrape-time collectors (read on each scrape, never cached): runs by status, evaluations by status, QUEUED/RETRYING/PENDING backlog and oldest-age, Redis stream length and pending count per stream, live workers.
- **Worker** exposes `:9464/metrics` (`AGENTEVAL_WORKER_METRICS_PORT`; 0 disables), served from a thread so it never blocks the event loop:
  - runs finished by `status` and `outcome`; attempt failures by `failure_reason` and category; retries (R8/R9), recoveries by transition code, dead letters by reason, cancellations by reason;
  - histograms: run duration, queue wait (created → claimed), evaluation duration; model call latency; tool call latency by tool;
  - counters: model tokens in/out and cost (USD) by provider/model; tool calls by tool and status; lease losses; artifact saves by result; reconciler step errors;
  - gauges: in-flight runs/gradings vs concurrency.
- **Cardinality rule:** labels are closed enums (status, outcome, reason, tool name, provider/model), never ids.
- **Compose profile `observability`:** Prometheus scraping the API and the worker (`infrastructure/monitoring/prometheus.yml`). Off by default.

### B. Traces: OpenTelemetry, exporter off by default

- **Why OpenTelemetry:** the vendor-neutral standard; the SDK is the only dependency (`opentelemetry-api`, `-sdk`, `-exporter-otlp-proto-http`).
- **Spans:** API request → `queue.publish` → (W3C `traceparent` carried as a field in the Redis stream message) → worker `run.execute` → `run.claim` / `attempt` → `agent.step` → `model.call` / `tool.call` → `artifact.save` → `run.finish` → `evaluation.grade` → `evaluation.check`. Attributes are ids and enums only; no prompts, outputs, hidden checks or file contents (D11, ADR-007).
- Every publish runs inside a `queue.publish` producer span and writes `traceparent`; a reconciler redispatch publishes inside its own `reconciler.redispatch` root span, so the redelivered run joins that trace. A message without `traceparent` (an older or unconfigured publisher) starts a new root `run.execute` span with a `run_id` attribute.
- **Export:** nothing leaves the process unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (standard OTel env vars). Compose profile `observability` adds Jaeger (OTLP in, UI on 16686).
- **Log correlation:** a structlog processor adds `trace_id`/`span_id` to every log line inside a span.

### C. Read API for the dashboard

List endpoints with keyset pagination for agents, tasks, datasets, evaluations and workers, and `GET /stats/overview` (evaluation counts, success rate, run latency p50/p95, total cost, active workers, queue depth). CORS allows the configured web origin only (`AGENTEVAL_CORS_ORIGINS`). Every response goes through the existing public schemas (D11).

### D. Dashboard: Next.js (`apps/web`)

- Stack per the project spec: Next.js (App Router) + TypeScript + Tailwind CSS + TanStack Query + Recharts. Information-dense, no decorative animation.
- Pages: Dashboard (KPIs + throughput/latency charts), Agents (versions, model, tools, benchmark results), Tasks, Evaluations (detail; compare A vs B vs C: success rate, latency, cost, failure categories), Run detail (attempt timeline, trajectory events, tool calls, errors, public evaluation and score).
- It talks only to the public REST API; it has no database access and no secrets. Compose service `web`.

## Consequences

- Three new Python dependencies (prometheus-client; OTel API/SDK/OTLP exporter), one new frontend app with its own toolchain (Node, npm).
- Metrics and spans are best-effort: an exporter or scrape failure never fails a run.
- Scrape-time collectors issue a few aggregate queries per scrape; at a 15 s scrape interval this is negligible against the run workload, and they use existing partial indexes.
- No authentication yet (Phase 8): `/metrics` and the dashboard are bound to localhost like the rest of the stack.

## Alternatives considered

| Option | Why not |
|---|---|
| Metrics derived only from Postgres (no client library) | No latency histograms for model/tool calls, which never hit the DB; scraping would be the only cost but the detail would be lost |
| StatsD / push gateway | An extra moving part; Prometheus pull is enough on one host |
| Grafana as the only dashboard | Good for operators, but the product needs agent/evaluation views over the API (CLAUDE.md §14); Prometheus remains usable from Grafana |
| Vendor APM SDK | Lock-in; OpenTelemetry exports to any backend |
