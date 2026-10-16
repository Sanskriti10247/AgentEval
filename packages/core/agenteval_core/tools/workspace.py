"""What tools need from the sandbox, independent of Docker.

The worker implements this with DockerWorkspace (a SandboxSession adapter);
unit tests use a scripted fake. Keeping it here means tools live in core with
no docker import, and the API can import their schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agenteval_core.reliability.taxonomy import FailureReason


class WorkspaceUnavailable(Exception):
    """The sandbox itself failed (daemon error, container gone, reaper failed).

    A platform fault: the runtime ends the attempt as SANDBOX_ERROR. Tools
    never catch it. `reason` carries the worker's SandboxError.reason when
    the failure came from one (never inferred from the message); it is None
    only for a raw Docker daemon error that never went through SandboxError.
    """

    def __init__(self, message: str, *, reason: FailureReason | None = None) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ExecOutput:
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    duration_ms: int = 0


class Workspace(Protocol):
    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        """Run argv in /workspace. On timeout the process group is killed and
        timed_out is set; the workspace stays usable."""
        ...

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        """Write files (relative paths) under /workspace. A non-zero exit
        (e.g. the path is a directory) is returned, not raised."""
        ...

    async def reap(self) -> None:
        """Kill every process a tool call left behind. Raises WorkspaceUnavailable."""
        ...

    async def abort(self) -> None:
        """Make any in-flight exec end now (used for cancellation). The
        workspace is unusable afterwards. Never raises."""
        ...
