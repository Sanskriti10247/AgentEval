"""Local sandbox backend: tasks run as plain subprocesses of the worker, no Docker.

For hosts with no Docker daemon (the single-container demo, docs/deployment.md "Free
hosting"). AGENTEVAL_SANDBOX_BACKEND=local picks it; the Docker backend stays the default.

THIS IS NOT A SECURITY BOUNDARY. Commands run as the worker's own user, on the worker's
network, next to the worker's Postgres and Redis credentials. What it gives is hygiene,
not isolation:

- a fresh directory per attempt (`<root>/run-<deadline>-<random>/{workspace,tmp,eval}`),
  removed when the attempt ends, and swept by its deadline like a Docker container;
- a scrubbed environment (fixed PATH, HOME and TMPDIR inside the run directory);
- per-exec rlimits: address space (the task's memory_mb), CPU seconds, file size (the
  workspace size), process count, no core files;
- every exec in its own session; on timeout the group gets SIGTERM, then SIGKILL a second
  later (what `timeout --kill-after=1` does in the container), and `reap()` kills whatever a
  call left behind, including processes that called setsid (found by an environment marker).

It is only safe where every command it runs is trusted: the public demo accepts runs of the
seeded agents and tasks only (AGENTEVAL_PUBLIC_DEMO).

Differences from the Docker backend, by design:

- the task's `image` is not used: commands run on this host's python3/coreutils. The
  attempt records `sandbox_image_id = "local"`;
- `/workspace` and `/eval` do not exist. Absolute references to them in argv, environment
  values and evaluator files are rewritten to the run's directories, and the run's
  directories are written back as `/workspace`, `/eval` and `/tmp` in any output, so
  trajectories read the same as in a container;
- no network isolation, no per-run disk quota beyond the per-file size limit, no OOM flag
  (an exec over its address-space limit fails with its own error instead).
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import re
import resource
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenteval_core.evaluation.results import ExecPlan, FileEvidence
from agenteval_core.evaluation.taxonomy import MAX_SNAPSHOT_BYTES
from agenteval_core.hashing import sha256_hex
from agenteval_core.logging import get_logger
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.schemas.task import validate_relative_path
from agenteval_core.states import Outcome
from agenteval_core.tools.workspace import ExecOutput, WorkspaceUnavailable
from agenteval_worker.sandbox.docker_sandbox import ExecutionResult, SandboxSpec
from agenteval_worker.sandbox.evaluation import (
    EVIDENCE_READ_CAP,
    SnapshotError,
    WorkspaceSnapshot,
    validate_snapshot,
)
from agenteval_worker.sandbox.session import (
    ORPHAN_GRACE_S,
    TOOL_EXEC_ENV,
    TOOL_KILL_AFTER_S,
    BoundedBuffer,
    ExecResult,
    SandboxError,
)

log = get_logger(__name__)

IMAGE_ID = "local"
LOCAL_PATH = "/usr/local/bin:/usr/bin:/bin"
MARKER_ENV = "AGENTEVAL_LOCAL_SANDBOX"
RUN_PREFIX = "run-"
# After the main process exits, how long an orphan may hold the output pipes open before
# its process group is killed (Docker closes the stream within ~2 s in the same case).
PIPE_GRACE_S = 2.0
SETUP_TIMEOUT_S = 60
# Headroom on RLIMIT_CPU over the wall-clock timeout: the wall clock is what ends a
# command; the CPU limit only catches a runaway that the timeout somehow missed.
CPU_HEADROOM_S = 2

_SANDBOX_PATH = re.compile(r"(?<![\w.\-/])/(workspace|eval|tmp)(?![\w.\-])")


@dataclass(frozen=True)
class _Limits:
    memory_mb: int
    cpu_s: int
    file_bytes: int
    nproc: int | None

    def apply(self) -> None:
        """preexec_fn: runs in the forked child before exec. Only setrlimit calls."""
        mem = self.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_s, self.cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (self.file_bytes, self.file_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if self.nproc is not None:
            resource.setrlimit(resource.RLIMIT_NPROC, (self.nproc, self.nproc))


def _user_task_count() -> int | None:
    """Threads and processes this uid already has. RLIMIT_NPROC counts them all, so the
    sandbox's allowance is added on top. None when /proc can't be read."""
    uid = os.getuid()
    count = 0
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == uid:
                    count += len(os.listdir(f"/proc/{entry.name}/task"))
            except OSError:
                continue
    except OSError:
        return None
    return count


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _force_rmtree(path: Path) -> None:
    """rmtree that also removes what was made read-only (a locked /eval, or a directory an
    agent chmod'ed)."""

    def retry(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        try:
            os.chmod(os.path.dirname(target), 0o700)
            if not os.path.islink(target):
                os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=retry)


def _wipe(directory: Path) -> None:
    for child in list(directory.iterdir()):
        if child.is_dir() and not child.is_symlink():
            _force_rmtree(child)
        else:
            child.unlink()
    if any(directory.iterdir()):
        raise SandboxError(
            f"could not reset {directory.name}", reason=FailureReason.SANDBOX_EXEC_FAILED
        )


class _CappedBuffer(io.BytesIO):
    """A write target that refuses to grow past `limit` (the compressed snapshot cap)."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self._limit:
            raise SnapshotError("too_large", f"compressed workspace exceeds {self._limit} bytes")
        return super().write(data)


class LocalSession:
    """One attempt's directory and every process started in it. Blocking, like
    SandboxSession: async callers go through LocalWorkspace / LocalEvaluationWorkspace."""

    def __init__(
        self,
        root: Path,
        *,
        lifetime_s: float,
        memory_mb: int,
        max_output_bytes: int,
        workspace_mb: int,
        pids_limit: int,
        path: str = LOCAL_PATH,
        with_eval: bool = False,
    ) -> None:
        deadline = time.time() + lifetime_s + ORPHAN_GRACE_S
        self.base = root / f"{RUN_PREFIX}{deadline:.0f}-{secrets.token_hex(6)}"
        self.workspace = self.base / "workspace"
        self.tmp = self.base / "tmp"
        self.eval_dir: Path | None = self.base / "eval" if with_eval else None
        for d in (self.workspace, self.tmp, self.eval_dir):
            if d is not None:
                d.mkdir(parents=True, mode=0o700)
        self._memory_mb = memory_mb
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_mb
        self._pids_limit = pids_limit
        self._path = path
        self._token = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._pgids: set[int] = set()
        self._killed = False

    # -- lifecycle ------------------------------------------------------------------------

    def kill(self) -> None:
        """Abort: every process of this session dies now and any later exec fails.
        Never raises."""
        with self._lock:
            self._killed = True
        try:
            self._kill_all()
        except Exception as exc:  # noqa: BLE001 - abort must not raise
            log.warning("local_sandbox_kill_failed", error=str(exc))

    def close(self) -> None:
        self.kill()
        try:
            _force_rmtree(self.base)
        except OSError as exc:
            log.warning("local_sandbox_remove_failed", path=str(self.base), error=str(exc))

    def reap(self) -> None:
        """Kill every process a call left behind (its group, and anything that left the
        group with setsid but still carries this session's marker)."""
        try:
            self._kill_all()
        except OSError as exc:
            raise SandboxError(
                f"reaper failed: {exc}", reason=FailureReason.SANDBOX_REAPER_FAILED
            ) from exc

    def _kill_all(self) -> None:
        with self._lock:
            pgids = list(self._pgids)
            self._pgids.clear()
        for pgid in pgids:
            _signal_group(pgid, signal.SIGKILL)
        self._kill_marked()

    def _kill_marked(self) -> int:
        needle = f"{MARKER_ENV}={self._token}".encode()
        killed = 0
        own = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == own:
                continue
            try:
                with open(f"/proc/{entry}/environ", "rb") as f:
                    environ = f.read()
            except OSError:
                continue
            if needle in environ.split(b"\0"):
                try:
                    os.kill(int(entry), signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
        return killed

    # -- paths ----------------------------------------------------------------------------

    def _to_host(self, text: str) -> str:
        """/workspace, /eval and /tmp as the command would write them -> this run's dirs."""
        dirs = {"workspace": self.workspace, "tmp": self.tmp}
        if self.eval_dir is not None:
            dirs["eval"] = self.eval_dir

        def sub(m: re.Match[str]) -> str:
            target = dirs.get(m.group(1))
            return str(target) if target is not None else m.group(0)

        return _SANDBOX_PATH.sub(sub, text)

    def _to_sandbox(self, text: str) -> str:
        """The reverse, for output: no host path of the run directory reaches a trajectory."""
        text = text.replace(str(self.workspace), "/workspace").replace(str(self.tmp), "/tmp")
        if self.eval_dir is not None:
            text = text.replace(str(self.eval_dir), "/eval")
        return text

    def _resolve_inside(self, rel: str) -> Path:
        """`rel` under the workspace, refusing any path whose parents leave it (a symlinked
        directory). GNU tar in the container refuses the same writes."""
        validate_relative_path(rel)
        target = self.workspace / rel
        parent = Path(os.path.realpath(target.parent))
        root = Path(os.path.realpath(self.workspace))
        if parent != root and root not in parent.parents:
            raise ValueError(f"{rel}: parent directory leaves the workspace")
        return target

    # -- files ----------------------------------------------------------------------------

    def write_files_result(
        self, files: dict[str, str], *, mtime: float | None = None
    ) -> ExecResult:
        """Write files under the workspace. Like the container's `tar -x`, a path that can't
        be written (a directory in the way, a symlink) is a non-zero exit, not an error."""
        started = time.monotonic()
        errors: list[str] = []
        for rel, content in files.items():
            try:
                target = self._resolve_inside(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, "wb") as f:
                    f.write(content.encode("utf-8"))
                if mtime is not None:
                    os.utime(target, (mtime, mtime), follow_symlinks=False)
            except (OSError, ValueError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                errors.append(f"tar: {rel}: Cannot open: {reason}")
        return ExecResult(
            exit_code=2 if errors else 0,
            stdout="",
            stderr="\n".join(errors),
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_ms=_elapsed_ms(started),
        )

    def write_files(self, files: dict[str, str], *, mtime: float | None = None) -> None:
        result = self.write_files_result(files, mtime=mtime)
        if result.exit_code != 0:
            raise SandboxError(
                f"extracting task files failed (exit {result.exit_code}): {result.stderr[-500:]!r}",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )

    # -- execs ----------------------------------------------------------------------------

    def exec_tool(self, argv: list[str], timeout_s: float) -> ExecResult:
        return self.run(argv, timeout_s=timeout_s, environment=TOOL_EXEC_ENV)[0]

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        environment: dict[str, str] | None = None,
        stdin: bytes | None = None,
        stdout_limit: int | None = None,
    ) -> tuple[ExecResult, BoundedBuffer]:
        """Run argv in the workspace. At timeout_s the process group gets SIGTERM, and
        SIGKILL TOOL_KILL_AFTER_S later; timed_out is set and the exit code is the one
        coreutils `timeout` would give (124, or 137 when KILL was needed)."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        with self._lock:
            if self._killed:
                raise SandboxError("the sandbox was aborted", reason=FailureReason.SANDBOX_LOST)
        started = time.monotonic()
        stdout = BoundedBuffer(stdout_limit if stdout_limit is not None else self._max_output)
        stderr = BoundedBuffer(self._max_output)
        env = self._environment(environment)
        argv = [self._to_host(a) for a in argv]
        tasks = _user_task_count()
        limits = _Limits(
            memory_mb=self._memory_mb,
            cpu_s=math.ceil(timeout_s) + CPU_HEADROOM_S,
            file_bytes=self._workspace_mb * 1024 * 1024,
            nproc=None if tasks is None else tasks + self._pids_limit,
        )
        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                # Python warns that preexec_fn can deadlock a child forked from a threaded
                # process. It only calls setrlimit; if it ever hung, the timeout below would
                # still kill the child's group, and the call would read as a timeout.
                preexec_fn=limits.apply,
                close_fds=True,
            )
        except OSError as exc:
            # What `timeout` reports in the container when it can't run the command.
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            stderr.append(f"failed to run command '{argv[0]}': {exc.strerror}\n".encode())
            return self._result(code, stdout, stderr, False, started), stdout
        with self._lock:
            self._pgids.add(proc.pid)
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
        ]
        if stdin is not None:
            readers.append(threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True))
        for t in readers:
            t.start()

        timed_out = False
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=TOOL_KILL_AFTER_S)
                code = 124
            except subprocess.TimeoutExpired:
                _signal_group(proc.pid, signal.SIGKILL)
                proc.wait()
                code = 137
        else:
            code = 128 - code if code < 0 else code
        # An orphan still holding the pipes ends with its group; one that left the group
        # (setsid) ends with the marker sweep.
        for kill in (lambda: _signal_group(proc.pid, signal.SIGKILL), self._kill_marked):
            if not _join(readers, PIPE_GRACE_S):
                kill()
        _join(readers, PIPE_GRACE_S)
        with self._lock:
            killed = self._killed
        if killed:
            raise SandboxError("the sandbox was aborted", reason=FailureReason.SANDBOX_LOST)
        return self._result(code, stdout, stderr, timed_out, started), stdout

    def _environment(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = {
            "PATH": self._path,
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
        }
        for key, value in (extra or {}).items():
            env[key] = self._to_host(value)
        env[MARKER_ENV] = self._token
        return env

    def _result(
        self,
        code: int | None,
        stdout: BoundedBuffer,
        stderr: BoundedBuffer,
        timed_out: bool,
        started: float,
    ) -> ExecResult:
        return ExecResult(
            exit_code=code,
            stdout=self._to_sandbox(stdout.text()),
            stderr=self._to_sandbox(stderr.text()),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            timed_out=timed_out,
            duration_ms=_elapsed_ms(started),
        )

    # -- the workspace snapshot -----------------------------------------------------------

    def export(
        self,
        *,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
        max_unpacked_bytes: int = 64 * 1024 * 1024,
    ) -> WorkspaceSnapshot:
        """C1, as export_workspace does in the container: unreadable entries are made
        owner-readable first (recorded), then a tar.gz of the whole workspace, complete or
        a SnapshotError. A member tar can't store (a socket) is `incomplete`."""
        buf = _CappedBuffer(max_bytes)
        try:
            normalized = self._normalize()
            with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                tar.add(self.workspace, arcname=".", recursive=False)
                for dirpath, dirnames, filenames in os.walk(self.workspace, onerror=_raise):
                    dirnames.sort()
                    rel_dir = os.path.relpath(dirpath, self.workspace)
                    for name in sorted(dirnames + filenames):
                        full = os.path.join(dirpath, name)
                        arcname = name if rel_dir == "." else f"{rel_dir}/{name}"
                        info = tar.gettarinfo(full, arcname=f"./{arcname}")
                        if info is None:
                            raise SnapshotError("incomplete", f"{arcname}: unsupported file type")
                        if info.isreg():
                            with open(full, "rb") as f:
                                tar.addfile(info, f)
                        else:
                            tar.addfile(info)
        except OSError as exc:
            raise SnapshotError("incomplete", f"export failed: {exc}") from exc
        data = buf.getvalue()
        validate_snapshot(data, max_unpacked_bytes=max_unpacked_bytes)
        return WorkspaceSnapshot(data=data, sha256=sha256_hex(data), normalized_paths=normalized)

    def _normalize(self) -> tuple[str, ...]:
        """`chmod -R u+rX`, recording each path whose mode changed."""
        changed: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.workspace, onerror=_raise):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    continue
                mode = stat.S_IMODE(st.st_mode)
                want = mode | stat.S_IRUSR
                if stat.S_ISDIR(st.st_mode) or mode & 0o111:
                    want |= stat.S_IXUSR
                if want != mode:
                    os.chmod(full, want)
                    changed.append(os.path.relpath(full, self.workspace))
            # os.walk lists a directory before descending, so a fixed mode lets it in.
        return tuple(sorted(changed))


class LocalEvaluationSession(LocalSession):
    """EvaluationSession's contract on a directory: /eval holds the hidden and evaluator
    files (read-only by mode, which the same user could undo: hygiene, not isolation), and
    the workspace is the snapshot again before every plan that asks for it."""

    def __init__(self, root: Path, snapshot: WorkspaceSnapshot, **kwargs: Any) -> None:
        super().__init__(root, with_eval=True, **kwargs)
        self.snapshot = snapshot
        self._dirty = True

    def install_eval_files(self, files: dict[str, str]) -> None:
        assert self.eval_dir is not None
        for rel, content in files.items():
            validate_relative_path(rel)
            target = self.eval_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._to_host(content), encoding="utf-8")
        for dirpath, _dirnames, filenames in os.walk(self.eval_dir, topdown=False):
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), 0o444)
            os.chmod(dirpath, 0o555)

    def restore_workspace(self) -> None:
        """/workspace := the snapshot, /tmp := empty (C5). The snapshot was validated
        header by header; the `tar` filter also keeps every member inside the workspace."""
        try:
            _wipe(self.workspace)
            _wipe(self.tmp)
            with tarfile.open(fileobj=io.BytesIO(self.snapshot.data), mode="r:gz") as tar:
                tar.extractall(self.workspace, filter="tar")
        except (OSError, tarfile.TarError) as exc:
            raise SandboxError(
                f"restoring the snapshot failed: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
            ) from exc
        self._dirty = False

    def run_plan(self, plan: ExecPlan) -> ExecResult:
        if plan.fresh_workspace and self._dirty:
            self.restore_workspace()
        self._dirty = True
        stdin = plan.stdin.encode() if plan.stdin is not None else None
        try:
            return self.run(
                list(plan.argv),
                timeout_s=plan.timeout_s,
                environment={**TOOL_EXEC_ENV, **plan.env},
                stdin=stdin,
            )[0]
        finally:
            self.reap()

    def collect_evidence(
        self, paths: list[str], *, read_cap: int = EVIDENCE_READ_CAP
    ) -> dict[str, FileEvidence]:
        if self._dirty:
            self.restore_workspace()
        evidence: dict[str, FileEvidence] = {}
        for path in paths:
            validate_relative_path(path)
            full = self.workspace / path
            if full.is_symlink() or (
                full.exists() and os.path.realpath(full) != os.path.abspath(full)
            ):
                evidence[path] = FileEvidence(path=path, exists=True, is_symlink=True)
            elif full.is_file():
                data = full.read_bytes()
                evidence[path] = FileEvidence(
                    path=path,
                    exists=True,
                    size=len(data),
                    sha256=sha256_hex(data),
                    content=data if len(data) <= read_cap else None,
                )
            elif full.exists():
                evidence[path] = FileEvidence(path=path, exists=True)
            else:
                evidence[path] = FileEvidence(path=path, exists=False)
        return evidence


class LocalSandbox:
    """The backend: opens sessions, runs S1 command tasks, and kills or sweeps by attempt
    like DockerSandbox does by container label."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_output_bytes: int = 64 * 1024,
        workspace_mb: int = 64,
        pids_limit: int = 128,
        path: str = LOCAL_PATH,
    ) -> None:
        base = Path(root) if root else Path(os.environ.get("TMPDIR", "/tmp"))
        self._path = path
        self.root = base / "agenteval-sandbox"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_mb
        self._pids_limit = pids_limit
        self._lock = threading.Lock()
        self._by_attempt: dict[str, set[LocalSession]] = {}

    def _session_kwargs(self, lifetime_s: float, memory_mb: int) -> dict[str, Any]:
        return {
            "lifetime_s": lifetime_s,
            "memory_mb": memory_mb,
            "max_output_bytes": self._max_output,
            "workspace_mb": self._workspace_mb,
            "pids_limit": self._pids_limit,
            "path": self._path,
        }

    def _new_session[S: LocalSession](
        self, cls: type[S], lifetime_s: float, memory_mb: int, *args: Any
    ) -> S:
        """A session's directories, or a SandboxError (a full or unwritable disk is the
        platform's failure, as a container that can't be created is)."""
        try:
            return cls(self.root, *args, **self._session_kwargs(lifetime_s, memory_mb))
        except OSError as exc:
            raise SandboxError(
                f"could not create the run directory: {exc}",
                reason=FailureReason.SANDBOX_CREATE_FAILED,
            ) from exc

    def _track(self, attempt_id: str | None, session: LocalSession) -> None:
        if attempt_id is not None:
            with self._lock:
                self._by_attempt.setdefault(attempt_id, set()).add(session)

    def _untrack(self, attempt_id: str | None, session: LocalSession) -> None:
        if attempt_id is not None:
            with self._lock:
                sessions = self._by_attempt.get(attempt_id)
                if sessions is not None:
                    sessions.discard(session)
                    if not sessions:
                        del self._by_attempt[attempt_id]

    def open_agent(
        self,
        files: dict[str, str],
        *,
        lifetime_s: float,
        memory_mb: int,
        attempt_id: str | None = None,
        image: str | None = None,
    ) -> LocalAgentSandbox:
        """The agent's session with the task files in place (real mtimes, C8)."""
        if image is not None:
            log.info("local_sandbox_image_ignored", image=image)
        session = self._new_session(LocalSession, lifetime_s, memory_mb)
        try:
            if files:
                session.write_files(files, mtime=time.time())
        except BaseException:
            session.close()
            raise
        self._track(attempt_id, session)
        return LocalAgentSandbox(self, session, attempt_id)

    def open_evaluation(
        self,
        snapshot: WorkspaceSnapshot,
        eval_files: dict[str, str],
        *,
        lifetime_s: float,
        memory_mb: int,
        attempt_id: str | None = None,
    ) -> LocalEvaluationSandbox:
        if sha256_hex(snapshot.data) != snapshot.sha256:
            raise SnapshotError("invalid", "content does not match its sha256")
        validate_snapshot(snapshot.data, max_unpacked_bytes=self._workspace_mb * 1024 * 1024)
        for path in eval_files:
            validate_relative_path(path)
        session = self._new_session(LocalEvaluationSession, lifetime_s, memory_mb, snapshot)
        try:
            session.install_eval_files(eval_files)
            session.restore_workspace()
        except OSError as exc:
            session.close()
            raise SandboxError(
                f"installing /eval failed: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
            ) from exc
        except BaseException:
            session.close()
            raise
        self._track(attempt_id, session)
        return LocalEvaluationSandbox(self, session, attempt_id)

    def release(self, attempt_id: str | None, session: LocalSession) -> None:
        self._untrack(attempt_id, session)
        session.close()

    def run(self, spec: SandboxSpec) -> ExecutionResult:
        """An S1 command task, DockerSandbox.run's contract: at the deadline everything
        the command started is killed (TIMEOUT); the directory always goes."""
        started = time.monotonic()
        attempt_id = spec.labels.get("agenteval.attempt_id")
        if spec.image:
            log.info("local_sandbox_image_ignored", image=spec.image)
        session: LocalSession | None = None
        try:
            session = self._new_session(LocalSession, spec.timeout_s, spec.memory_mb)
            self._track(attempt_id, session)
            if spec.files:
                session.write_files(spec.files, mtime=0)
            result, _ = session.run(spec.command, timeout_s=spec.timeout_s)
            if result.timed_out:
                outcome = Outcome.TIMEOUT
            elif result.exit_code == 0:
                outcome = Outcome.SUCCESS
            else:
                outcome = Outcome.NON_ZERO_EXIT
            return ExecutionResult(
                outcome=outcome,
                exit_code=result.exit_code,
                oom_killed=False,
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_truncated=result.stdout_truncated,
                stderr_truncated=result.stderr_truncated,
                duration_ms=_elapsed_ms(started),
            )
        except SandboxError as exc:
            return _error(started, str(exc), exc.reason)
        except OSError as exc:
            return _error(
                started, f"local sandbox error: {exc}", FailureReason.SANDBOX_CREATE_FAILED
            )
        finally:
            if session is not None:
                self.release(attempt_id, session)

    def kill_attempt(self, attempt_id: str) -> int:
        """Lease lost or cancel requested: abort every session of this attempt. Their
        directories go when their owners close them (or with the sweep)."""
        with self._lock:
            sessions = list(self._by_attempt.get(attempt_id, ()))
        for session in sessions:
            session.kill()
        return len(sessions)

    def sweep_orphans(self) -> int:
        """Remove run directories past the deadline in their name (as DockerSandbox does
        with its deadline label), so a crashed worker's leftovers go too."""
        now = time.time()
        removed = 0
        with self._lock:
            live = {s.base for group in self._by_attempt.values() for s in group}
        for entry in self.root.iterdir():
            if not entry.name.startswith(RUN_PREFIX) or entry in live:
                continue
            try:
                deadline = float(entry.name[len(RUN_PREFIX) :].split("-", 1)[0])
            except ValueError:
                deadline = 0.0
            if deadline < now:
                _force_rmtree(entry)
                removed += 1
        return removed


class LocalAgentSandbox:
    """What AgentRunner needs from an agent's sandbox (see agent_runner._AgentSandbox)."""

    def __init__(self, owner: LocalSandbox, session: LocalSession, attempt_id: str | None) -> None:
        self._owner = owner
        self.session = session
        self._attempt_id = attempt_id
        self.workspace = LocalWorkspace(session)

    def image_id(self) -> str | None:
        return IMAGE_ID

    def export(
        self, *, max_bytes: int, max_unpacked_bytes: int, timeout_s: float
    ) -> WorkspaceSnapshot:
        self.session.reap()
        return self.session.export(max_bytes=max_bytes, max_unpacked_bytes=max_unpacked_bytes)

    def close(self) -> None:
        self._owner.release(self._attempt_id, self.session)


class LocalEvaluationSandbox:
    def __init__(
        self, owner: LocalSandbox, session: LocalEvaluationSession, attempt_id: str | None
    ) -> None:
        self._owner = owner
        self.session = session
        self._attempt_id = attempt_id
        self.workspace = LocalEvaluationWorkspace(session)

    def close(self) -> None:
        self._owner.release(self._attempt_id, self.session)


class LocalWorkspace:
    """core's Workspace protocol over a LocalSession (DockerWorkspace's counterpart)."""

    def __init__(self, session: LocalSession) -> None:
        self.session = session

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        return _to_output(await _call(self.session.exec_tool, argv, timeout_s))

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        return _to_output(
            await _call(lambda: self.session.write_files_result(files, mtime=time.time()))
        )

    async def reap(self) -> None:
        await _call(self.session.reap)

    async def abort(self) -> None:
        await asyncio.to_thread(self.session.kill)


class LocalEvaluationWorkspace:
    def __init__(self, session: LocalEvaluationSession) -> None:
        self.session = session

    async def run_plan(self, plan: ExecPlan) -> ExecOutput:
        return _to_output(await _call(self.session.run_plan, plan))

    async def collect_evidence(self, paths: list[str]) -> dict[str, FileEvidence]:
        return await _call(self.session.collect_evidence, paths)

    async def abort(self) -> None:
        await asyncio.to_thread(self.session.kill)


async def _call[T](fn: Callable[..., T], *args: object) -> T:
    try:
        return await asyncio.to_thread(fn, *args)
    except SandboxError as exc:
        raise WorkspaceUnavailable(str(exc), reason=exc.reason) from exc
    except OSError as exc:
        raise WorkspaceUnavailable(
            f"local sandbox error: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
        ) from exc


def _drain(pipe: Any, buffer: BoundedBuffer) -> None:
    try:
        while chunk := pipe.read1(65536):
            buffer.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _feed(pipe: Any, data: bytes) -> None:
    try:
        pipe.write(data)
    except (OSError, ValueError):
        pass  # the process exited or was killed before reading it all
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _raise(exc: OSError) -> None:
    raise exc


def _join(threads: Iterable[threading.Thread], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in threads)


def _to_output(r: ExecResult) -> ExecOutput:
    return ExecOutput(
        exit_code=r.exit_code,
        stdout=r.stdout,
        stderr=r.stderr,
        stdout_truncated=r.stdout_truncated,
        stderr_truncated=r.stderr_truncated,
        timed_out=r.timed_out,
        duration_ms=r.duration_ms,
    )


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
