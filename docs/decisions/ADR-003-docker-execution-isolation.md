# ADR-003: Docker for execution isolation

**Status:** Accepted (vertical slice)

## Context
Tasks (and, in Phase 3, agents) run commands that must be treated as untrusted. They must never run inside the API process or directly on the host.

## Decision
Each attempt gets one ephemeral **sibling** container, created by the worker through the Docker SDK over the mounted Docker socket. The container is created with:
- `network_mode=none`
- `mem_limit` = `memswap_limit` (no swap), `nano_cpus`, `pids_limit=128`
- `read_only=True` rootfs, with size-capped `tmpfs` at `/workspace` (64 MB) and `/tmp` (16 MB)
- `user=1000:1000`, `cap_drop=[ALL]`, `security_opt=[no-new-privileges]`, `init=True`
- labels `agenteval.managed`, `agenteval.run_id`, `agenteval.attempt_id` and `agenteval.deadline`

The execution sequence is:
1. The container starts on `sleep infinity`.
2. Task files are streamed in as a tar over `exec` stdin into `tar -x`.
3. The command runs via `exec`, and stdout and stderr are streamed into 64 KB bounded buffers.
4. A watchdog thread kills the container at `timeout_s`.
5. The container is always removed in `finally`.

Why a long-lived container plus exec: it is exactly the shape the Phase 3 agent needs, where each tool call is another exec into the same workspace.

**File copy deviates from the plan.** The plan said `put_archive`. Docker rejects it (`container rootfs is marked read-only`) when `read_only=True`, which was verified in a spike. Streaming a tar over exec stdin keeps the read-only rootfs.

**Orphan sweep.** At worker start, any managed container whose `agenteval.deadline` has passed is removed. The sweep is deadline-based rather than worker-based, so one worker's sweep can never remove a container another live worker is using.

## Alternatives
- `docker run` per command: loses workspace state between tool calls.
- gVisor or Firecracker: much stronger isolation, but heavy on macOS. Future work.
- A host subprocess: forbidden.

## Consequences
- **The mounted socket is host-root equivalent.** The worker is a trusted component, and it runs as root in its container for that reason. The API image does not even install the Docker SDK.
- The kernel is shared with the host. This is a research sandbox, not a multi-tenant security boundary.
