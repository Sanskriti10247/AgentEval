# ADR-005: Hand-written agent runtime on the existing worker

**Status:** Accepted (Phase 3, implemented)

## Context

Phase 3 adds the thing the platform exists to measure: an agent that solves a task by calling a model and using tools inside the sandbox. Every run must be:

- **Attributable:** a result belongs to one exact model and configuration.
- **Bounded:** in steps, tokens, money and wall-clock time.
- **Observable:** every model call and tool call is recorded.
- **Reproducible enough to compare runs.**

The loop has to be simple enough to explain, with no framework hiding it.

## Decision

An **agent task** runs through the same S1 pipeline as a command task:

```text
queue -> conditional claim -> attempt -> execute -> fenced persist
```

Only `execute` differs. For agent tasks it is `AgentRunner`:

```text
AgentRunner (worker)
  validate the agent version (provider enabled, model priced, limits, tools)
  SandboxSession.open_for_tools      one container for the whole attempt
  provider.start(...)                -> ModelSession (owns the transcript)
  AgentRuntime.run(ctx)              the loop, in core, provider/Docker-agnostic
     sink = TrajectoryWriter         Postgres, fenced to the owning worker
  session.close()                    always, shielded from cancellation
-> finish_attempt(outcome, steps, tokens, cost, final answer)
```

### The loop (`agenteval_core/agent/runtime.py`)

```text
run_started
loop:
    stop if cancelled / past the deadline / max_steps / token budget / cost budget
    model call (in-step retries on retryable provider errors)  -> model_call
    refusal -> MODEL_REFUSAL     unexpected stop -> MODEL_ERROR
    no tool calls -> AGENT_FINISHED (final answer)
    each tool call, sequentially                                -> tool_call
    answer all tool calls in one message
run_finished
```

- **Every exit records `run_finished`.** Exits happen through one internal `_Stop(outcome, reason)`.
- **Events are flushed four times:** after `run_started`, after each model response (before its tools run), after each step's tool calls, and at the end.
- **A trajectory-write failure stops the run** before any more money is spent.

### Outcomes and run status

`status_for()` remains the single mapping, and DB CHECK constraints mirror it (migration `0002`). **COMPLETED** means the attempt produced a trustworthy result about the agent. **FAILED** means the agent was never fairly tested.

| Outcome | When | Status |
| --- | --- | --- |
| `AGENT_FINISHED` | the model ended its turn without tool calls (it wins even if that turn crossed a budget) | COMPLETED |
| `MAX_STEPS` | the step limit was reached | COMPLETED |
| `BUDGET_EXCEEDED` | the token or USD budget was reached (which one is in the reason) | COMPLETED |
| `TIMEOUT` | the wall clock ran out while the model or a tool was working normally; this includes a provider timeout bounded by the deadline | COMPLETED |
| `MODEL_REFUSAL` | `stop_reason: refusal`; the category is recorded | COMPLETED |
| `MODEL_ERROR` | provider errors exhausted the retries or the remaining time; a non-retryable error; an unexpected stop reason | FAILED |
| `SANDBOX_ERROR` | the sandbox could not be opened, or was lost mid-run (including reaper failure) | FAILED |
| `INTERNAL_ERROR` | cancellation (reason recorded, e.g. `cancelled: worker_shutdown`), an invalid or unsupported config at run time, a bug | FAILED |

Whether the agent actually *solved* the task is Phase 4's evaluator, not the outcome (see ADR-006).

### Model providers

- **`ModelProvider` → `ModelSession` is provider-neutral.** The session owns the transcript, and the runtime exchanges only tool results and text in, and `ModelTurn` out (text, tool calls, stop reason, usage, the model that actually served the call).
  - The transcript is append-only by construction, so provider blocks that must be replayed verbatim, such as thinking blocks, stay inside the provider.
  - `PendingToolCalls` enforces the conversation rules: every tool call must be answered exactly once before the next model call. A violation raises `ProtocolViolation` instead of reaching the API as a 400.
- **`FakeProvider` is the default for development, CI and every standard test.**
  - It replays a validated, JSON-serialisable script (turns, errors, delays, checks on tool results).
  - Tool-call IDs, usage and latency are deterministic.
  - Agent versions carry the script in `model_settings`, so end-to-end runs cost nothing.
- **`AnthropicProvider` (worker package only) is opt-in** (`AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER`, default false, plus `ANTHROPIC_API_KEY`).
  - **What each request contains:**
    - a system block with a cache breakpoint, plus top-level automatic caching
    - tools with `strict: true`
    - `thinking: {type: "adaptive"}` and `output_config.effort` where the model supports them
  - **Deliberately absent:**
    - SDK retries (`max_retries=0`, enforced at construction)
    - refusal fallbacks
    - forced `tool_choice`
    - betas and streaming
  - **Errors** are mapped by HTTP status onto `ModelErrorKind`. `RETRYABLE_KINDS` is the single retry classification: 429, 529, other 5xx, 408/504, timeouts, connection errors and 409.
- **Runtime-owned retries.** `next_model_retry()` is a pure function:
  - backoff `min(2s·2^n, 30s)` plus jitter, at most 3 retries
  - the server's `retry-after` wins, but never past the deadline
  - every failed try becomes a `model_error` event
- **Cost** comes from an explicit per-model price table (verified 2026-09-28): input, 5-minute cache write, cache read and output, each stored separately because cache-read multipliers differ between models. Raw token counts are stored too, so cost can be recomputed.

### Tools and the sandbox

- **Six built-in tools** (`read_file`, `write_file`, `list_dir`, `search`, `shell`, `run_tests`) live in core and depend only on a `Workspace` protocol. `DockerWorkspace` adapts `SandboxSession` and maps sandbox failures to `WorkspaceUnavailable`.
- **`ToolSet` is an agent version's allowlist,** minus `run_tests` when the task has no test command.
  - `invoke()` validates input against the tool's Pydantic model.
  - Unknown tools, invalid input, denied commands and failures become `is_error` results the agent can react to.
  - Output is capped at 16 KiB for the model and 64 KiB in the trajectory.
- **Model-supplied values reach the sandbox only as argv elements or positional `sh` parameters,** never inside a shell string. `shell` takes an argv list whose `argv[0]` must be on the version's allowlist (bare names, no paths).
- **One container per attempt, reused across tool calls.** Each tool command runs as:

  ```text
  timeout --kill-after=1 <n> <argv...>
  ```

  where `n = min(tool_timeout_s, time left)`. A host-side backstop kills the container if `timeout` ever fails to fire.
- **No process outlives its tool call.** After every executed call, a POSIX `sh` reaper SIGKILLs every process except PID 1 (tini), the keepalive (recorded when the session opens) and itself. The §15 spike showed why `timeout` alone isn't enough: background children survive once their shell exits, and `setsid` escapes the process group. A reaper failure is a `SandboxError`.

### Persistence

Migration `0002` adds:

- `agents`, and immutable `agent_versions`, which are unique on `config_hash`
- `trajectory_events`, unique on `(attempt_id, seq)`
- the agent task type, `runs.agent_version_id`, and per-attempt aggregates (steps, tokens, cost, final answer)

Trajectory batches are written by `append_fenced()`:

- **Fenced:** they are written only while this worker owns a RUNNING attempt, which is the `finish_attempt` fence, checked under `FOR SHARE` on the run row.
- **Idempotent:** `ON CONFLICT (attempt_id, seq) DO NOTHING`.
- **Retried:** transient DB errors are retried with a 0.2 / 0.5 / 1 s backoff. Losing ownership is never retried.

The trajectory stores operational data only: model usage, visible text, tool inputs and outputs, errors, limits. **Thinking blocks are never persisted.**

### Cancellation and shutdown

- **`CancelToken`** (an event plus a reason) is checked between steps and raced against model calls, backoff sleeps and tool calls.
- **Cancelling a tool call** calls `Workspace.abort()`, which kills the container, then waits (bounded) for the call to unwind.
- **Worker shutdown keeps the S1 total of `drain_timeout_s` (330 s), in phases:**
  1. wait until `drain_timeout - cancel_grace` (310 s)
  2. cancel in-flight agent runs with reason `worker_shutdown`, and give them `cancel_grace` (20 s) to record and persist
  3. hard-cancel the rest, as S1 does

## Why

- **The loop is the thing being evaluated.** Owning it (about 400 lines including the event plumbing) keeps limits, retries, cancellation and the trajectory explicit and testable. A framework would hide exactly the behaviour we measure.
- **Reusing the S1 pipeline** means agent runs inherit the conditional claim, ack ordering, fencing and status/outcome semantics that were already tested, and command tasks keep their behaviour.
- **The neutral provider boundary** keeps `anthropic` out of core and out of the API image, and makes `FakeProvider` a drop-in. Every behaviour except the HTTP transport is tested without a network or a key.

## Alternatives considered

| Option | Why not |
| --- | --- |
| SDK Tool Runner / Claude Agent SDK / an agent framework | Hides the loop, retries and limits we need to control and record |
| Managed Agents | Anthropic would host the loop and the sandbox; ours is the subject of ADR-003 |
| A separate agent service | No scaling or isolation need yet; another network hop (ADR-004) |
| Parallel tool execution | Ordering of writes to one workspace becomes non-deterministic |
| SDK-level retries | Retries would be invisible in the trajectory |
| Refusal fallbacks on | A run could be served by a different model than the version names |

## Consequences and limits

- **No background processes across tool calls.** An agent can't start a server in one call and query it in the next.
- **Docker holds the output stream open for about 2 s** when a tool leaves a process holding stdout. This was observed in the spike, and the reaper cleans up afterwards.
- **Budgets are checked between model calls,** so the worst case is overshooting by one call, whose output is bounded by `max_tokens`.
- **Calls are non-streaming.** Very large `max_tokens_per_call` values may approach HTTP timeouts. Streaming is future work.
- **Prices are list-price estimates:** no batch or volume discounts, no `inference_geo` multiplier, 5-minute cache writes only.
- **A worker crash mid-run still leaves the run RUNNING** until Phase 5 adds leases. The trajectory written up to that point is kept.

## Deviations from the approved design (found while implementing)

1. **A process reaper was added after the §15 spike.** `timeout` alone missed background children whose shell had exited, and `setsid` escapees.
2. **`Workspace.abort()` was added** so cancellation can interrupt a tool call that's in progress.
3. **Tool calls run with `PYTHONDONTWRITEBYTECODE=1`, and tool writes use real mtimes.** CPython validates `.pyc` files by size plus mtime in whole seconds, so a same-size edit made within the same second as an earlier import ran stale code: a timing-dependent result. S1 command execution is unchanged.
4. **Tool schemas are sent as a strict-compatible copy.** Strict tool use rejects numeric, length and array constraints, so they are removed and described in words. `ToolSet` still validates the full schema.
5. **The effort level is stored explicitly** (default `high`) in an Anthropic version's settings, so the config hash says exactly what is sent (model defaults differ: Opus 5.5 defaults to `medium`). Haiku 4.5 takes no effort or thinking parameters.
6. **A latent S1 consumer bug was fixed.** The loop checked for shutdown only after acquiring a concurrency slot, so with every slot busy the drain did not start at SIGTERM.
7. **Over-cap limits are rejected (422), not clamped,** so a version always runs with exactly its stored config.
