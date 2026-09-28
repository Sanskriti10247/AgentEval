"""Docker sandbox: one ephemeral container per attempt.

Lifecycle (all blocking; the worker calls `run` via asyncio.to_thread):

1. create a locked-down container on an idle entrypoint (`sleep infinity`)
2. stream the task files in as a tar over `exec` stdin into `tar -x`
   (`put_archive` is rejected by Docker when the rootfs is read-only)
3. exec the task command, streaming stdout/stderr into bounded buffers
4. a watchdog thread kills the container at `timeout_s` (-> TIMEOUT)
5. always remove the container

Security note: this is a research sandbox, not a hardened multi-tenant boundary.
See README "Security model".
"""

from __future__ import annotations

import io
import socket
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import docker
import docker.errors
from docker.models.containers import Container

from agenteval_core.logging import get_logger
from agenteval_core.states import Outcome

log = get_logger(__name__)

MANAGED_LABEL = "agenteval.managed"
DEADLINE_LABEL = "agenteval.deadline"
WORKSPACE = "/workspace"
SANDBOX_USER = "1000:1000"
# Extra lifetime allowed beyond the task timeout (create, file copy, cleanup)
# before a leftover container is considered orphaned.
ORPHAN_GRACE_S = 120
FILE_COPY_TIMEOUT_S = 30


class SandboxError(Exception):
    """The platform could not set up or drive the sandbox (-> SANDBOX_ERROR)."""


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


class BoundedBuffer:
    """Keeps the first `limit` bytes of a stream and records whether more arrived.

    The rest of the stream is still drained (and discarded) by the caller so the
    process is never blocked on a full pipe.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()
        self.truncated = False

    def append(self, chunk: bytes | None) -> None:
        if not chunk:
            return
        room = self._limit - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]
        if len(chunk) > room:
            self.truncated = True

    def text(self) -> str:
        # Postgres text cannot hold NUL; invalid UTF-8 (e.g. a cut multibyte
        # char at the limit) becomes U+FFFD.
        return self._buf.decode("utf-8", errors="replace").replace("\x00", "�")


def build_tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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
        container: Container | None = None
        try:
            container = self._create(spec)
            container.start()
            if spec.files:
                self._copy_files(container, spec.files)
            return self._exec(container, spec, started)
        except SandboxError as exc:
            return self._error(started, str(exc))
        except docker.errors.ImageNotFound as exc:
            return self._error(started, f"image not found: {spec.image} ({exc.explanation})")
        except docker.errors.DockerException as exc:
            return self._error(started, f"docker error: {exc}")
        finally:
            if container is not None:
                self._remove(container)

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
                self._remove(c)
                removed += 1
        return removed

    # -- steps ------------------------------------------------------------------

    def _create(self, spec: SandboxSpec) -> Container:
        mem = f"{spec.memory_mb}m"
        deadline = time.time() + spec.timeout_s + ORPHAN_GRACE_S
        # containers.create never pulls, so a missing image raises ImageNotFound.
        return self._client.containers.create(
            spec.image,
            command=["sleep", "infinity"],
            init=True,  # tini as PID 1 reaps exec'd children
            network_mode="none",
            mem_limit=mem,
            memswap_limit=mem,  # no swap: memory limit is a hard limit
            nano_cpus=int(spec.cpu_limit * 1e9),
            pids_limit=self._pids_limit,
            read_only=True,
            tmpfs={
                WORKSPACE: f"rw,size={self._workspace_mb}m,uid=1000,gid=1000,mode=0755",
                "/tmp": "rw,size=16m,mode=1777",
            },
            user=SANDBOX_USER,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            working_dir=WORKSPACE,
            labels={**spec.labels, MANAGED_LABEL: "true", DEADLINE_LABEL: f"{deadline:.0f}"},
            detach=True,
        )

    def _copy_files(self, container: Container, files: dict[str, str]) -> None:
        api = self._client.api
        exec_id = api.exec_create(
            container.id,
            ["tar", "-x", "--no-same-owner", "-C", WORKSPACE, "-f", "-"],
            stdin=True,
            user=SANDBOX_USER,
        )["Id"]
        conn: Any = api.exec_start(exec_id, socket=True)
        raw: socket.socket = conn._sock
        try:
            raw.settimeout(FILE_COPY_TIMEOUT_S)
            raw.sendall(build_tar(files))
            raw.shutdown(socket.SHUT_WR)
            output = b""
            while chunk := raw.recv(65536):
                output += chunk[:4096]
        except OSError as exc:
            raise SandboxError(f"copying task files failed: {exc}") from exc
        finally:
            conn.close()
        exit_code = self._wait_exec_exit(exec_id)
        if exit_code != 0:
            raise SandboxError(
                f"extracting task files failed (exit {exit_code}): {output[-500:]!r}"
            )

    def _exec(self, container: Container, spec: SandboxSpec, started: float) -> ExecutionResult:
        api = self._client.api
        exec_id = api.exec_create(container.id, spec.command, user=SANDBOX_USER)["Id"]

        # Whoever takes the lock first decides: the watchdog (-> TIMEOUT) or
        # the normal end of the output stream. This avoids labelling a command
        # that finished just before the deadline as TIMEOUT.
        lock = threading.Lock()
        state = {"finished": False, "timed_out": False}

        def watchdog() -> None:
            with lock:
                if state["finished"]:
                    return
                state["timed_out"] = True
            try:
                container.kill()
            except docker.errors.DockerException:
                pass  # already gone

        timer = threading.Timer(spec.timeout_s, watchdog)
        timer.daemon = True
        stdout, stderr = BoundedBuffer(self._max_output), BoundedBuffer(self._max_output)
        timer.start()
        try:
            for out_chunk, err_chunk in api.exec_start(exec_id, stream=True, demux=True):
                stdout.append(out_chunk)
                stderr.append(err_chunk)
        finally:
            with lock:
                state["finished"] = True
            timer.cancel()

        exit_code = self._wait_exec_exit(exec_id)
        container.reload()
        oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled", False))

        if state["timed_out"]:
            outcome = Outcome.TIMEOUT
        elif exit_code is None:
            raise SandboxError("could not determine the command's exit code")
        elif exit_code == 0:
            outcome = Outcome.SUCCESS
        else:
            outcome = Outcome.NON_ZERO_EXIT

        return ExecutionResult(
            outcome=outcome,
            exit_code=exit_code,
            oom_killed=oom_killed,
            stdout=stdout.text(),
            stderr=stderr.text(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            duration_ms=_elapsed_ms(started),
        )

    # -- helpers ----------------------------------------------------------------

    def _wait_exec_exit(self, exec_id: str) -> int | None:
        # The stream can close a moment before Docker records the exit code.
        for _ in range(50):
            info = self._client.api.exec_inspect(exec_id)
            if not info.get("Running"):
                return info.get("ExitCode")
            time.sleep(0.02)
        return None

    def _remove(self, container: Container) -> None:
        # Never let cleanup failure mask the execution result; a leftover
        # container is caught later by sweep_orphans via its deadline label.
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as exc:
            log.warning("sandbox_remove_failed", container_id=container.id, error=str(exc))

    @staticmethod
    def _error(started: float, message: str) -> ExecutionResult:
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
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
