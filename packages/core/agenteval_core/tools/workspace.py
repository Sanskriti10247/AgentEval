"""What tools need from the sandbox, independent of Docker.

The worker implements this with DockerWorkspace (a SandboxSession adapter);
unit tests use a scripted fake. Keeping it here means tools live in core with
no docker import, and the API can import their schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class WorkspaceUnavailable(Exception):
    """The sandbox itself failed (daemon error, container gone, reaper failed).

    A platform fault: the runtime ends the attempt as SANDBOX_ERROR. Tools
    never catch it.
    """


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
