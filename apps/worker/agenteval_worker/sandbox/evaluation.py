"""Evaluation-container support (Phase 4 step 4), built on SandboxSession.

    agent container (SandboxSession, after the agent finished and was reaped)
        export_workspace()  -> WorkspaceSnapshot (tar.gz, complete or SnapshotError)

    evaluation container (EvaluationSession: same hardened settings, plus)
        - every tmpfs explicitly noexec,nosuid,nodev
        - /eval: hidden + evaluator files, written as root, then locked
          (dirs 0555, files 0444, root-owned): readable by the candidate's
          uid 1000, never writable, and /eval/.nopyc uncreatable (C2)
        - /workspace: the snapshot, restored before every plan that asks for a
          fresh workspace, with /tmp wiped (C5)
        - run_plan(ExecPlan): the Step 3 plans through the same timeout
          wrapper, backstop and reaper as agent tool calls
        - collect_evidence(paths): file facts from the pristine snapshot only

Never extracted on the host: snapshots are validated header-by-header here
and only ever unpacked by tar inside a sandbox, as uid 1000.

ExecResults returned by run_plan carry raw process output for the Step 3
judge to parse; they are transient and never persisted or returned (D11).
"""

from __future__ import annotations

import io
import re
import tarfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Literal

import docker
import docker.errors

from agenteval_core.evaluation.checks import EVAL_DIR
from agenteval_core.evaluation.results import ExecPlan, FileEvidence
from agenteval_core.evaluation.taxonomy import MAX_SNAPSHOT_BYTES
from agenteval_core.hashing import sha256_hex
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.schemas.task import validate_relative_path
from agenteval_worker.sandbox.session import (
    SANDBOX_USER,
    TOOL_EXEC_ENV,
    WORKSPACE,
    ContainerSpec,
    ExecResult,
    SandboxError,
    SandboxSession,
    build_tar,
    container_kwargs,
)

ROOT_USER = "0:0"
EVAL_TMPFS_MB = 8
TMP_TMPFS_MB = 16
SETUP_TIMEOUT_S = 60
MAX_SNAPSHOT_MEMBERS = 20_000
EVIDENCE_READ_CAP = 64 * 1024
# Explicit, not left to Docker's defaults (the spike observed these defaults).
HARDENED_MOUNT = "rw,noexec,nosuid,nodev"
EVALUATION_ROLE_LABEL = "agenteval.role"

# C1: the owner restores read/search bits so tar can read everything it owns;
# -c reports each change so the evaluation can record what was normalised.
_NORMALIZE = "cd /workspace && chmod -R -c u+rX ."
_NORMALIZED_LINE = re.compile(r"^mode of '\./(.+)' changed from")
_LOCK_EVAL = (
    "chmod -R a-w /eval && find /eval -type d -exec chmod 0555 {} + "
    "&& find /eval -type f -exec chmod 0444 {} +"
)
# The candidate's uid must not be able to write /eval, and /eval must be root's.
_VERIFY_LOCK = 'test ! -w /eval && [ "$(stat -c %u:%a /eval)" = "0:555" ]'
_WIPE = (
    "chmod -R u+rwX /workspace /tmp 2>/dev/null; "
    "find /workspace /tmp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + ; "
    '[ -z "$(find /workspace /tmp -mindepth 1 -print -quit)" ]'
)
# Classify one /workspace path without following a final symlink, and treat a
# path whose parents resolve elsewhere (a symlinked directory) as a symlink.
_EVIDENCE = (
    'p="/workspace/$1"; '
    'if [ -L "$p" ]; then echo symlink; '
    'elif [ -e "$p" ] && [ "$(readlink -f -- "$p")" != "$p" ]; then echo symlink; '
    'elif [ -f "$p" ]; then echo "file $(stat -c %s -- "$p") '
    '$(sha256sum -- "$p" | cut -d\' \' -f1)"; '
    'elif [ -e "$p" ]; then echo other; '
    "else echo missing; fi"
)


class SnapshotError(SandboxError):
    """A workspace snapshot could not be produced or trusted. Never silent.

    `reason` here is the O3d workspace_capture literal, not a FailureReason:
    SnapshotError only ever happens on a COMPLETED outcome (A2, an evaluator
    concern), so it never feeds the attempt's failure_reason. The base
    class's FailureReason is a placeholder, immediately shadowed below.
    """

    def __init__(self, reason: Literal["too_large", "incomplete", "invalid"], message: str) -> None:
        super().__init__(f"snapshot {reason}: {message}", reason=FailureReason.SANDBOX_EXEC_FAILED)
        self.reason: Literal["too_large", "incomplete", "invalid"] = reason  # type: ignore[assignment]


@dataclass(frozen=True)
class WorkspaceSnapshot:
    data: bytes  # tar.gz of /workspace
    sha256: str
    normalized_paths: tuple[str, ...] = field(default_factory=tuple)  # C1

    @property
    def size(self) -> int:
        return len(self.data)


# -- export (from the agent's container) -----------------------------------------------


def export_workspace(
    session: SandboxSession,
    *,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
    max_unpacked_bytes: int = 64 * 1024 * 1024,
    timeout_s: float = SETUP_TIMEOUT_S,
) -> WorkspaceSnapshot:
    """C1: complete or an explicit SnapshotError - never a silently partial
    snapshot. Unreadable files are made owner-readable first (recorded), then
    any tar failure, an over-cap archive or an invalid archive raises."""
    norm = session._exec(["sh", "-c", _NORMALIZE], deadline_s=timeout_s, on_deadline=session.kill)
    if norm.timed_out or norm.exit_code != 0:
        raise SnapshotError("incomplete", f"normalising permissions failed: {norm.stdout[-300:]!r}")
    normalized = sorted(
        m.group(1) for line in norm.stdout.splitlines() if (m := _NORMALIZED_LINE.match(line))
    )

    result, raw = session._run(
        ["tar", "-czf", "-", "-C", WORKSPACE, "."],
        deadline_s=timeout_s,
        on_deadline=session.kill,
        stdout_limit=max_bytes,
    )
    if result.timed_out:
        raise SnapshotError("incomplete", f"export did not finish within {timeout_s:g}s")
    if raw.truncated:
        raise SnapshotError("too_large", f"compressed workspace exceeds {max_bytes} bytes")
    # Any diagnostic counts: GNU tar skips some members (unix sockets: "socket
    # ignored") with only a warning and exit 0, which would be a silent loss.
    if result.exit_code != 0 or result.stderr.strip():
        raise SnapshotError(
            "incomplete", f"tar exited {result.exit_code}: {result.stderr[-300:]!r}"
        )
    data = raw.data()
    validate_snapshot(data, max_unpacked_bytes=max_unpacked_bytes)
    return WorkspaceSnapshot(data=data, sha256=sha256_hex(data), normalized_paths=tuple(normalized))


def validate_snapshot(
    data: bytes, *, max_unpacked_bytes: int, max_members: int = MAX_SNAPSHOT_MEMBERS
) -> None:
    """Header-only validation on the host (never extracts). Defence in depth
    for stored snapshots: rejects absolute or '..' names, device nodes,
    hardlinks to anything outside the archive, unknown member types, too many
    members, and more unpacked data than the workspace can hold."""
    seen: set[str] = set()
    total = count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tar:
            for member in tar:
                count += 1
                if count > max_members:
                    raise SnapshotError("invalid", f"more than {max_members} members")
                name = _clean(member.name)
                if name is None:
                    raise SnapshotError("invalid", f"unsafe member name {member.name!r}")
                if member.ischr() or member.isblk():
                    raise SnapshotError("invalid", f"device node {member.name!r}")
                if member.islnk():
                    target = _clean(member.linkname)
                    if target is None or target not in seen:
                        raise SnapshotError(
                            "invalid", f"hardlink {member.name!r} escapes the archive"
                        )
                elif not (member.isreg() or member.isdir() or member.issym() or member.isfifo()):
                    raise SnapshotError("invalid", f"unsupported member type {member.name!r}")
                if member.isreg():
                    total += member.size
                    if total > max_unpacked_bytes:
                        raise SnapshotError(
                            "invalid", f"unpacks to more than {max_unpacked_bytes} bytes"
                        )
                seen.add(name)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise SnapshotError("invalid", f"unreadable archive: {exc}") from exc


def _clean(name: str) -> str | None:
    if name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts) or "."


# -- the evaluation container ---------------------------------------------------------------


def evaluation_container_kwargs(
    spec: ContainerSpec, *, workspace_tmpfs_mb: int, pids_limit: int, eval_tmpfs_mb: int
) -> dict[str, Any]:
    """The agent container's settings (ADR-003), plus a root-owned /eval and
    explicit noexec,nosuid,nodev on every writable mount."""
    kwargs = container_kwargs(spec, workspace_tmpfs_mb=workspace_tmpfs_mb, pids_limit=pids_limit)
    kwargs["tmpfs"] = {
        WORKSPACE: f"{HARDENED_MOUNT},size={workspace_tmpfs_mb}m,uid=1000,gid=1000,mode=0755",
        "/tmp": f"{HARDENED_MOUNT},size={TMP_TMPFS_MB}m,mode=1777",
        EVAL_DIR: f"{HARDENED_MOUNT},size={eval_tmpfs_mb}m,uid=0,gid=0,mode=0755",
    }
    kwargs["labels"] = {**kwargs["labels"], EVALUATION_ROLE_LABEL: "evaluation"}
    return kwargs


class EvaluationSession(SandboxSession):
    def __init__(
        self,
        client: docker.DockerClient,
        container: Any,
        max_output: int,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        super().__init__(client, container, max_output)
        self.snapshot = snapshot
        self._dirty = True  # nothing restored yet

    @classmethod
    def open(
        cls,
        client: docker.DockerClient,
        spec: ContainerSpec,
        snapshot: WorkspaceSnapshot,
        eval_files: dict[str, str],
        *,
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
        eval_tmpfs_mb: int = EVAL_TMPFS_MB,
    ) -> EvaluationSession:
        """Validate the snapshot, create the hardened container, install and
        lock /eval, restore /workspace. Removes the container on any failure."""
        if sha256_hex(snapshot.data) != snapshot.sha256:
            raise SnapshotError("invalid", "content does not match its sha256")
        validate_snapshot(snapshot.data, max_unpacked_bytes=workspace_tmpfs_mb * 1024 * 1024)
        for path in eval_files:
            validate_relative_path(path)
        kwargs = evaluation_container_kwargs(
            spec,
            workspace_tmpfs_mb=workspace_tmpfs_mb,
            pids_limit=pids_limit,
            eval_tmpfs_mb=eval_tmpfs_mb,
        )
        try:
            container = client.containers.create(spec.image, **kwargs)
        except docker.errors.ImageNotFound as exc:
            raise SandboxError(
                f"image not found: {spec.image} ({exc.explanation})",
                reason=FailureReason.SANDBOX_IMAGE_NOT_FOUND,
            ) from exc
        except docker.errors.DockerException as exc:
            raise SandboxError(
                f"docker error: {exc}", reason=FailureReason.SANDBOX_CREATE_FAILED
            ) from exc
        session = cls(client, container, max_output_bytes, snapshot)
        try:
            session.start()
            session._record_keepalive()
            session._install_eval_files(eval_files)
            session.restore_workspace()
        except BaseException:
            session.close()
            raise
        return session

    # -- workspace state --------------------------------------------------------------

    def restore_workspace(self) -> None:
        """/workspace := the snapshot, /tmp := empty (C5). Run as uid 1000."""
        wipe = self._exec(["sh", "-c", _WIPE], deadline_s=SETUP_TIMEOUT_S, on_deadline=self.kill)
        if wipe.timed_out or wipe.exit_code != 0:
            raise SandboxError(
                f"could not reset the workspace (exit {wipe.exit_code})",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
        restored = self._tar_in(self.snapshot.data, WORKSPACE, SANDBOX_USER, compressed=True)
        if restored.exit_code != 0:
            raise SandboxError(
                f"restoring the snapshot failed (exit {restored.exit_code}): "
                f"{restored.stderr[-300:]!r}",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
        self._dirty = False

    # -- running checks ---------------------------------------------------------------

    def run_plan(self, plan: ExecPlan) -> ExecResult:
        """Run one Step 3 ExecPlan: optional fresh workspace, timeout wrapper,
        stdin, plan env; the reaper always runs afterwards."""
        if plan.fresh_workspace and self._dirty:
            self.restore_workspace()
        self._dirty = True
        environment = {**TOOL_EXEC_ENV, **plan.env}
        stdin = plan.stdin.encode() if plan.stdin is not None else None
        try:
            return self._exec_wrapped(
                list(plan.argv), plan.timeout_s, environment=environment, stdin=stdin
            )
        finally:
            self.reap()

    def collect_evidence(
        self, paths: list[str], *, read_cap: int = EVIDENCE_READ_CAP
    ) -> dict[str, FileEvidence]:
        """File facts for static checks and protected paths, always taken from
        the pristine snapshot (restored first if any plan has run)."""
        if self._dirty:
            self.restore_workspace()
        evidence: dict[str, FileEvidence] = {}
        for path in paths:
            validate_relative_path(path)
            meta = self._exec(
                ["sh", "-c", _EVIDENCE, "evidence", path],
                deadline_s=SETUP_TIMEOUT_S,
                on_deadline=self.kill,
            )
            if meta.timed_out or meta.exit_code != 0:
                raise SandboxError(
                    f"could not inspect {path!r} (exit {meta.exit_code})",
                    reason=FailureReason.SANDBOX_EXEC_FAILED,
                )
            kind, *rest = meta.stdout.split()
            if kind == "missing":
                evidence[path] = FileEvidence(path=path, exists=False)
            elif kind == "symlink":
                evidence[path] = FileEvidence(path=path, exists=True, is_symlink=True)
            elif kind == "other":  # a directory, fifo, ...: never read
                evidence[path] = FileEvidence(path=path, exists=True)
            else:
                size, digest = int(rest[0]), rest[1]
                content = None
                if size <= read_cap:
                    result, raw = self._run(
                        ["cat", "--", f"{WORKSPACE}/{path}"],
                        deadline_s=SETUP_TIMEOUT_S,
                        on_deadline=self.kill,
                        stdout_limit=read_cap,
                    )
                    if result.exit_code != 0 or sha256_hex(raw.data()) != digest:
                        raise SandboxError(
                            f"reading {path!r} gave inconsistent content",
                            reason=FailureReason.SANDBOX_EXEC_FAILED,
                        )
                    content = raw.data()
                evidence[path] = FileEvidence(
                    path=path, exists=True, size=size, sha256=digest, content=content
                )
        return evidence

    # -- internals -----------------------------------------------------------------------

    def _install_eval_files(self, files: dict[str, str]) -> None:
        installed = self._tar_in(build_tar(files, mtime=time.time()), EVAL_DIR, ROOT_USER)
        if installed.exit_code != 0:
            raise SandboxError(
                f"installing /eval failed (exit {installed.exit_code})",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
        locked, _ = self._run(
            ["sh", "-c", _LOCK_EVAL],
            deadline_s=SETUP_TIMEOUT_S,
            on_deadline=self.kill,
            user=ROOT_USER,
        )
        if locked.exit_code != 0:
            raise SandboxError(
                f"locking /eval failed (exit {locked.exit_code})",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
        verified = self._exec(
            ["sh", "-c", _VERIFY_LOCK], deadline_s=SETUP_TIMEOUT_S, on_deadline=self.kill
        )
        if verified.exit_code != 0:
            raise SandboxError(
                "/eval is not root-owned and read-only to the candidate",
                reason=FailureReason.SANDBOX_EXEC_FAILED,
            )
