"""Docker sandbox for S1 command tasks: one ephemeral container per attempt.

Lifecycle (all blocking; the worker calls `run` via asyncio.to_thread):

1. create a locked-down container on an idle entrypoint (`sleep infinity`)
2. stream the task files in as a tar over `exec` stdin into `tar -x`
   (`put_archive` is rejected by Docker when the rootfs is read-only)
3. exec the task command, streaming stdout/stderr into bounded buffers
4. a watchdog thread kills the container at `timeout_s` (-> TIMEOUT)
5. always remove the container

The container primitives live in session.py (shared with agent tasks); this
module keeps the S1 behaviour and public names unchanged.

Security note: this is a research sandbox, not a hardened multi-tenant boundary.
See README "Security model".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import docker
import docker.errors

from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome
from agenteval_worker.sandbox.session import (
    DEADLINE_LABEL,
    MANAGED_LABEL,
    OOM_SETTLE_S,
    BoundedBuffer,
    ContainerSpec,
    SandboxError,
    SandboxSession,
    build_tar,
)

__all__ = [
    "DEADLINE_LABEL",
    "MANAGED_LABEL",
    "BoundedBuffer",
    "DockerSandbox",
    "ExecutionResult",
    "SandboxError",
    "SandboxSpec",
    "build_tar",
]


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    command: list[str]
    files: dict[str, str]
    timeout_s: int
    memory_mb: int
    cpu_limit: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    outcome: Outcome
    exit_code: int | None
    oom_killed: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    error_message: str | None = None
    # Set exactly for a failed outcome (SANDBOX_ERROR here); None for a
    # successful run. Never inferred from error_message - DockerSandbox.run
    # maps each failure by its exception type.
    failure_reason: FailureReason | None = None


class DockerSandbox:
    def __init__(
        self,
        client: docker.DockerClient,
        *,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
    ) -> None:
        self._client = client
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_tmpfs_mb
        self._pids_limit = pids_limit

    # -- public -----------------------------------------------------------------

    def run(self, spec: SandboxSpec) -> ExecutionResult:
        started = time.monotonic()
        session: SandboxSession | None = None
        try:
            session = SandboxSession.create(
                self._client,
                ContainerSpec(
                    image=spec.image,
                    lifetime_s=spec.timeout_s,
                    memory_mb=spec.memory_mb,
                    cpu_limit=spec.cpu_limit,
                    labels=spec.labels,
                ),
                max_output_bytes=self._max_output,
                workspace_tmpfs_mb=self._workspace_mb,
                pids_limit=self._pids_limit,
            )
            session.start()
            if spec.files:
                session.write_files(spec.files)
            result = session.exec_until_deadline(spec.command, spec.timeout_s)
            oom_killed = session.oom_killed(
                settle_s=OOM_SETTLE_S if result.exit_code == 137 else 0.0
            )

            if result.timed_out:
                outcome = Outcome.TIMEOUT
            elif result.exit_code is None:
                raise SandboxError(
                    "could not determine the command's exit code",
                    reason=FailureReason.SANDBOX_EXEC_FAILED,
                )
            elif result.exit_code == 0:
                outcome = Outcome.SUCCESS
            else:
                outcome = Outcome.NON_ZERO_EXIT

            return ExecutionResult(
                outcome=outcome,
                exit_code=result.exit_code,
                oom_killed=oom_killed,
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_truncated=result.stdout_truncated,
                stderr_truncated=result.stderr_truncated,
                duration_ms=_elapsed_ms(started),
            )
        except SandboxError as exc:
            # DockerSandbox maps errors by exception type, never by message
            # (G15/3.8): a SandboxError already carries its own reason.
            return self._error(started, str(exc), exc.reason)
        except docker.errors.ImageNotFound as exc:
            return self._error(
                started,
                f"image not found: {spec.image} ({exc.explanation})",
                FailureReason.SANDBOX_IMAGE_NOT_FOUND,
            )
        except docker.errors.DockerException as exc:
            return self._error(started, f"docker error: {exc}", FailureReason.SANDBOX_CREATE_FAILED)
        finally:
            if session is not None:
                session.close()

    def kill_attempt(self, attempt_id: str) -> int:
        """Lease lost (design §6 rule 3): kill and remove every container
        labelled for this attempt, agent or command task alike. Best-effort
        and idempotent - the container may already be gone, or never have
        been created. Called from a thread; this worker no longer owns the
        attempt and makes no further Postgres writes either way."""
        killed = 0
        for c in self._client.containers.list(
            all=True, filters={"label": f"agenteval.attempt_id={attempt_id}"}
        ):
            try:
                c.kill()
            except docker.errors.APIError:
                pass
            try:
                c.remove(force=True)
            except docker.errors.APIError:
                pass
            killed += 1
        return killed

    def sweep_orphans(self) -> int:
        """Remove managed containers past their deadline label.

        Deadline-based (not worker-based) so one worker's startup sweep can
        never remove a container another live worker is still using.
        """
        now = time.time()
        removed = 0
        for c in self._client.containers.list(all=True, filters={"label": MANAGED_LABEL}):
            try:
                deadline = float(c.labels.get(DEADLINE_LABEL, "0"))
            except ValueError:
                deadline = 0.0
            if deadline < now:
                SandboxSession(self._client, c, self._max_output).close()
                removed += 1
        return removed

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _error(started: float, message: str, reason: FailureReason) -> ExecutionResult:
        return ExecutionResult(
            outcome=Outcome.SANDBOX_ERROR,
            exit_code=None,
            oom_killed=False,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=_elapsed_ms(started),
            error_message=message,
            failure_reason=reason,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
