"""SandboxSession: one locked-down container, many execs.

S1 command tasks use it for a single exec whose deadline kills the container
(`exec_until_deadline`). Agent tasks keep the container alive for the whole
attempt and run each tool call through `exec_tool`, which:

1. wraps the argv as `timeout --kill-after=1 <n> <argv...>` so the command's
   process group is TERMed at the deadline and KILLed one second later;
2. arms a host-side backstop that kills the container if the exec still
   hasn't finished well past that (it should never fire);

and the caller then runs `reap()`, which SIGKILLs every process except PID 1
(tini), the keepalive and the reaper itself. The §15 spike showed `timeout`
alone misses background children once the shell exits and anything that
calls setsid; the reaper catches both. Rule: no process outlives its tool
call.

All methods block; async callers use asyncio.to_thread.
"""

from __future__ import annotations

import io
import socket
import struct
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import docker
import docker.errors
from docker.models.containers import Container

from agenteval_core.logging import get_logger
from agenteval_core.reliability.taxonomy import FailureReason

log = get_logger(__name__)

MANAGED_LABEL = "agenteval.managed"
DEADLINE_LABEL = "agenteval.deadline"
WORKSPACE = "/workspace"
SANDBOX_USER = "1000:1000"
# /proc/<pid>/cmdline of the keepalive with NULs turned into spaces.
KEEPALIVE_CMDLINE = "sleep infinity "
# Extra lifetime allowed beyond the task timeout (create, file copy, cleanup)
# before a leftover container is considered orphaned.
ORPHAN_GRACE_S = 120
FILE_COPY_TIMEOUT_S = 30
# Tool exec: `timeout` TERMs at n, KILLs at n+1; Docker closes the stream up
# to ~2s after the main process exits if an orphan still holds the pipe
# (observed in the spike). The backstop fires only well beyond all of that.
TOOL_KILL_AFTER_S = 1
TOOL_BACKSTOP_MARGIN_S = 8
REAP_TIMEOUT_S = 10
# Environment for tool execs only (S1 execs are unchanged). No bytecode cache:
# CPython validates .pyc files by source size + mtime in whole seconds, so an
# agent's same-size edit within the same second as a previous import would run
# stale code - timing-dependent results, which a benchmark must not have.
TOOL_EXEC_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}

# Kill every process except PID 1 (tini), the keepalive ($1) and this shell.
# POSIX sh builtins only, so it works in any image with /bin/sh.
REAPER_SCRIPT = (
    "for d in /proc/[0-9]*; do p=${d#/proc/}; "
    '[ "$p" = 1 ] || [ "$p" = "$1" ] || [ "$p" = "$$" ] || kill -9 "$p" 2>/dev/null; '
    "done; exit 0"
)


class SandboxError(Exception):
    """The platform could not set up or drive the sandbox (-> SANDBOX_ERROR).

    `reason` is a closed FailureReason (never inferred from the message), set
    at each raise site below so the attempt's stored failure_reason is exact.
    """

    def __init__(self, message: str, *, reason: FailureReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    lifetime_s: int  # how long the container may legitimately live (deadline label)
    memory_mb: int
    cpu_limit: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int


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

    def data(self) -> bytes:
        """The raw bytes kept (for binary streams such as a snapshot)."""
        return bytes(self._buf)


class _FrameDemuxer:
    """Incremental version of demux_stream for a live attach socket."""

    def __init__(self, stdout: BoundedBuffer, stderr: BoundedBuffer) -> None:
        self._pending = bytearray()
        self._out, self._err = stdout, stderr

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        while len(self._pending) >= 8:
            size = struct.unpack(">I", self._pending[4:8])[0]
            if len(self._pending) < 8 + size:
                return
            payload = bytes(self._pending[8 : 8 + size])
            (self._err if self._pending[0] == 2 else self._out).append(payload)
            del self._pending[: 8 + size]


def build_tar(files: dict[str, str], *, mtime: float = 0) -> bytes:
    """mtime=0 (1970) is the S1 behaviour for task files, kept unchanged.
    Tool writes pass the real time so tools that compare mtimes (make, test
    caches) see an edit as an edit."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(mtime)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def demux_stream(raw: bytes) -> tuple[bytes, bytes]:
    """Split Docker's multiplexed attach stream (8-byte frame headers:
    stream id, 3 pad bytes, big-endian length) into (stdout, stderr)."""
    out, err = bytearray(), bytearray()
    i = 0
    while i + 8 <= len(raw):
        stream_id, size = raw[i], struct.unpack(">I", raw[i + 4 : i + 8])[0]
        payload = raw[i + 8 : i + 8 + size]
        (err if stream_id == 2 else out).extend(payload)
        i += 8 + size
    return bytes(out), bytes(err)


def container_kwargs(
    spec: ContainerSpec, *, workspace_tmpfs_mb: int, pids_limit: int
) -> dict[str, Any]:
    """The S1 container settings (ADR-003), shared by every sandbox."""
    mem = f"{spec.memory_mb}m"
    deadline = time.time() + spec.lifetime_s + ORPHAN_GRACE_S
    return {
        "command": ["sleep", "infinity"],
        "init": True,  # tini as PID 1 reaps exec'd children
        "network_mode": "none",
        "mem_limit": mem,
        "memswap_limit": mem,  # no swap: memory limit is a hard limit
        "nano_cpus": int(spec.cpu_limit * 1e9),
        "pids_limit": pids_limit,
        "read_only": True,
        "tmpfs": {
            WORKSPACE: f"rw,size={workspace_tmpfs_mb}m,uid=1000,gid=1000,mode=0755",
            "/tmp": "rw,size=16m,mode=1777",
        },
        "user": SANDBOX_USER,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "working_dir": WORKSPACE,
        "labels": {**spec.labels, MANAGED_LABEL: "true", DEADLINE_LABEL: f"{deadline:.0f}"},
        "detach": True,
    }


class SandboxSession:
    def __init__(self, client: docker.DockerClient, container: Container, max_output: int) -> None:
        self._client = client
        self.container = container
        self._max_output = max_output
        self._keepalive_pid: str | None = None

    # -- lifecycle ---------------------------------------------------------------

    @classmethod
    def create(
        cls,
        client: docker.DockerClient,
        spec: ContainerSpec,
        *,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
    ) -> SandboxSession:
        """Create (not start) the container. containers.create never pulls, so
        a missing image raises docker.errors.ImageNotFound."""
        container = client.containers.create(
            spec.image,
            **container_kwargs(spec, workspace_tmpfs_mb=workspace_tmpfs_mb, pids_limit=pids_limit),
        )
        return cls(client, container, max_output_bytes)

    @classmethod
    def open_for_tools(
        cls,
        client: docker.DockerClient,
        spec: ContainerSpec,
        files: dict[str, str],
        *,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
    ) -> SandboxSession:
        """Create, start, copy files, record the keepalive. Removes the
        container again if any step fails. Raises SandboxError."""
        try:
            session = cls.create(
                client,
                spec,
                max_output_bytes=max_output_bytes,
                workspace_tmpfs_mb=workspace_tmpfs_mb,
                pids_limit=pids_limit,
            )
        except docker.errors.ImageNotFound as exc:
            raise SandboxError(
                f"image not found: {spec.image} ({exc.explanation})",
                reason=FailureReason.SANDBOX_IMAGE_NOT_FOUND,
            ) from exc
        except docker.errors.DockerException as exc:
            raise SandboxError(
                f"docker error: {exc}", reason=FailureReason.SANDBOX_CREATE_FAILED
            ) from exc
        try:
            session.start()
            if files:
                # C8: agent-task files get the real time, so mtime-based tools
                # (make, test caches) see later edits as edits. S1 command
                # tasks (DockerSandbox.run) keep their original mtime 0.
                session.write_files(files, mtime=time.time())
            session._record_keepalive()
        except BaseException:
            session.close()
            raise
        return session

    def start(self) -> None:
        self.container.start()

    def kill(self) -> None:
        try:
            self.container.kill()
        except docker.errors.DockerException:
            pass  # already gone

    def close(self) -> None:
        # Never let cleanup failure mask a result; a leftover container is
        # caught later by sweep_orphans via its deadline label.
        try:
            self.container.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as exc:
            log.warning("sandbox_remove_failed", container_id=self.container.id, error=str(exc))

    @property
    def image_id(self) -> str | None:
        """The ID (content digest) of the image this container actually runs,
        resolved by Docker - stored for reproducibility. A tag can later point
        elsewhere; this ID can't."""
        self.container.reload()
        return self.container.attrs.get("Image")

    def oom_killed(self) -> bool:
        self.container.reload()
        return bool(self.container.attrs.get("State", {}).get("OOMKilled", False))

    # -- files -------------------------------------------------------------------

    def write_files_result(self, files: dict[str, str], *, mtime: float = 0) -> ExecResult:
        """Stream files as a tar over exec stdin into `tar -x` under /workspace
        (put_archive is rejected on a read-only rootfs). A non-zero tar exit is
        returned, not raised: for a tool it's the agent's problem (e.g. the
        path is a directory). Transport failures raise SandboxError."""
        return self._tar_in(build_tar(files, mtime=mtime), WORKSPACE, SANDBOX_USER)

    def _tar_in(
        self, archive: bytes, dest: str, user: str, *, compressed: bool = False
    ) -> ExecResult:
        """Stream an archive into `tar -x` at dest, run as user. GNU tar's own
        protections apply (leading '/' and '..' stripped, no write-through
        symlinks); see the Phase 4 spike. Transport failures raise SandboxError."""
        started = time.monotonic()
        api = self._client.api
        flags = "-xz" if compressed else "-x"
        try:
            exec_id = api.exec_create(
                self.container.id,
                ["tar", flags, "--no-same-owner", "-C", dest, "-f", "-"],
                stdin=True,
                user=user,
            )["Id"]
            conn: Any = api.exec_start(exec_id, socket=True)
        except docker.errors.DockerException as exc:
            raise SandboxError(
                f"copying files failed: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
            ) from exc
        raw: socket.socket = conn._sock
        output = bytearray()
        try:
            raw.settimeout(FILE_COPY_TIMEOUT_S)
            raw.sendall(archive)
            raw.shutdown(socket.SHUT_WR)
            while chunk := raw.recv(65536):
                if len(output) < 65536:
                    output += chunk
        except OSError as exc:
            raise SandboxError(
                f"copying files failed: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
            ) from exc
        finally:
            conn.close()
        out, err = demux_stream(bytes(output))
        return ExecResult(
            exit_code=self._wait_exec_exit(exec_id),
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_ms=_elapsed_ms(started),
        )

    def write_files(self, files: dict[str, str], *, mtime: float = 0) -> None:
        """As write_files_result, but any failure is a SandboxError (task files
        are platform input: if they can't be written the task wasn't tested)."""
        result = self.write_files_result(files, mtime=mtime)
        if result.exit_code != 0:
            raise SandboxError(
                f"extracting task files failed (exit {result.exit_code}): {result.stderr[-500:]!r}",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )

    # -- execs -------------------------------------------------------------------

    def exec_until_deadline(self, argv: list[str], timeout_s: float) -> ExecResult:
        """S1 semantics: at the deadline the whole container is killed."""
        return self._exec(argv, deadline_s=timeout_s, on_deadline=self.kill)

    def exec_tool(self, argv: list[str], timeout_s: float) -> ExecResult:
        """Run one tool command; the container survives a timeout.

        The in-container `timeout` enforces timeout_s; the host backstop only
        exists in case that somehow fails, and turns it into a SandboxError.
        """
        return self._exec_wrapped(argv, timeout_s, environment=TOOL_EXEC_ENV)

    def _exec_wrapped(
        self,
        argv: list[str],
        timeout_s: float,
        *,
        environment: dict[str, str],
        stdin: bytes | None = None,
    ) -> ExecResult:
        """The timeout-wrapped exec shared by tool calls and evaluation plans."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        wrapped = ["timeout", f"--kill-after={TOOL_KILL_AFTER_S}", _seconds(timeout_s), *argv]
        backstop = timeout_s + TOOL_KILL_AFTER_S + TOOL_BACKSTOP_MARGIN_S
        result, _ = self._run(
            wrapped,
            deadline_s=backstop,
            on_deadline=self.kill,
            environment=environment,
            stdin=stdin,
        )
        if result.timed_out:  # the backstop fired: the container is gone
            raise SandboxError(
                f"tool exec overran its {backstop:.0f}s backstop; container killed",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
        # `timeout` exits 124 (TERM worked) or 137 (needed KILL) when it fired.
        timed_out = result.exit_code in (124, 137) and result.duration_ms >= timeout_s * 1000
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            timed_out=timed_out,
            duration_ms=result.duration_ms,
        )

    def reap(self) -> None:
        """SIGKILL everything except PID 1, the keepalive and the reaper.
        Any failure is a SandboxError: we can no longer promise a clean
        workspace for the next tool call."""
        if self._keepalive_pid is None:
            raise SandboxError(
                "reap() called before the keepalive was recorded",
                reason=FailureReason.SANDBOX_REAPER_FAILED,
            )
        try:
            result = self._exec(
                ["sh", "-c", REAPER_SCRIPT, "reaper", self._keepalive_pid],
                deadline_s=REAP_TIMEOUT_S,
                on_deadline=self.kill,
            )
        except docker.errors.DockerException as exc:
            raise SandboxError(
                f"reaper failed: {exc}", reason=FailureReason.SANDBOX_REAPER_FAILED
            ) from exc
        if result.timed_out or result.exit_code != 0:
            raise SandboxError(
                f"reaper failed (exit {result.exit_code}, timed_out={result.timed_out}): "
                f"{result.stderr[-300:]!r}",
                reason=FailureReason.SANDBOX_REAPER_FAILED,
            )

    # -- internals ---------------------------------------------------------------

    def _record_keepalive(self) -> None:
        """Must run before any tool exec: later, reparented orphans are also
        children of PID 1 and could be mistaken for the keepalive."""
        try:
            children = self._exec(
                ["cat", "/proc/1/task/1/children"], deadline_s=REAP_TIMEOUT_S, on_deadline=self.kill
            ).stdout.split()
            if len(children) != 1:
                raise SandboxError(
                    f"expected one child of PID 1, found {children}",
                    reason=FailureReason.SANDBOX_LOST,
                )
            cmdline = self._exec(
                ["sh", "-c", "tr '\\0' ' ' < /proc/$1/cmdline", "cmdline", children[0]],
                deadline_s=REAP_TIMEOUT_S,
                on_deadline=self.kill,
            ).stdout
        except docker.errors.DockerException as exc:
            raise SandboxError(
                f"could not inspect the sandbox processes: {exc}", reason=FailureReason.SANDBOX_LOST
            ) from exc
        if cmdline != KEEPALIVE_CMDLINE:
            raise SandboxError(
                f"PID 1's child is not the keepalive: {cmdline!r}",
                reason=FailureReason.SANDBOX_LOST,
            )
        self._keepalive_pid = children[0]

    def _exec(
        self,
        argv: list[str],
        *,
        deadline_s: float,
        on_deadline: Any,
        environment: dict[str, str] | None = None,
    ) -> ExecResult:
        """Exec and stream output into bounded buffers. If the stream is still
        open at deadline_s, on_deadline() runs (it must end the stream)."""
        return self._run(
            argv, deadline_s=deadline_s, on_deadline=on_deadline, environment=environment
        )[0]

    def _run(
        self,
        argv: list[str],
        *,
        deadline_s: float,
        on_deadline: Any,
        environment: dict[str, str] | None = None,
        user: str = SANDBOX_USER,
        stdin: bytes | None = None,
        stdout_limit: int | None = None,
    ) -> tuple[ExecResult, BoundedBuffer]:
        """_exec plus: run as another user, feed stdin, and return the raw
        stdout buffer (stdout_limit overrides the text cap, for binary output)."""
        started = time.monotonic()
        api = self._client.api
        exec_id = api.exec_create(
            self.container.id, argv, user=user, environment=environment, stdin=stdin is not None
        )["Id"]

        # Whoever takes the lock first decides: the watchdog or the normal end
        # of the output stream. A command finishing just before the deadline
        # is therefore never labelled as timed out.
        lock = threading.Lock()
        state = {"finished": False, "timed_out": False}

        def watchdog() -> None:
            with lock:
                if state["finished"]:
                    return
                state["timed_out"] = True
            on_deadline()

        timer = threading.Timer(deadline_s, watchdog)
        timer.daemon = True
        stdout = BoundedBuffer(stdout_limit if stdout_limit is not None else self._max_output)
        stderr = BoundedBuffer(self._max_output)
        timer.start()
        try:
            if stdin is None:
                for out_chunk, err_chunk in api.exec_start(exec_id, stream=True, demux=True):
                    stdout.append(out_chunk)
                    stderr.append(err_chunk)
            else:
                self._stream_with_stdin(exec_id, stdin, stdout, stderr)
        finally:
            with lock:
                state["finished"] = True
            timer.cancel()

        result = ExecResult(
            exit_code=self._wait_exec_exit(exec_id),
            stdout=stdout.text(),
            stderr=stderr.text(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            timed_out=state["timed_out"],
            duration_ms=_elapsed_ms(started),
        )
        return result, stdout

    def _stream_with_stdin(
        self, exec_id: str, data: bytes, stdout: BoundedBuffer, stderr: BoundedBuffer
    ) -> None:
        """Attach a socket, send stdin from a thread (a process that writes
        before reading can't deadlock us), and demux output as it arrives. The
        caller's watchdog ends the stream by killing the container."""
        conn: Any = self._client.api.exec_start(exec_id, socket=True)
        raw: socket.socket = conn._sock

        def send() -> None:
            try:
                raw.sendall(data)
                raw.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # the process exited or was killed before reading it all

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        demux = _FrameDemuxer(stdout, stderr)
        try:
            while chunk := raw.recv(65536):
                demux.feed(chunk)
        except OSError:
            pass  # stream ended by the watchdog's kill
        finally:
            sender.join(timeout=1)
            conn.close()

    def _wait_exec_exit(self, exec_id: str) -> int | None:
        # The stream can close a moment before Docker records the exit code.
        for _ in range(50):
            info = self._client.api.exec_inspect(exec_id)
            if not info.get("Running"):
                return info.get("ExitCode")
            time.sleep(0.02)
        return None


def _seconds(value: float) -> str:
    """Format a duration for coreutils `timeout` (accepts decimals)."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
