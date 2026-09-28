"""DockerWorkspace: adapts a SandboxSession to core's Workspace protocol.

Blocking Docker calls run in a thread. Sandbox failures are translated into
WorkspaceUnavailable so core (the runtime and tools) never imports docker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TypeVar

import docker.errors

from agenteval_core.tools.workspace import ExecOutput, WorkspaceUnavailable
from agenteval_worker.sandbox.session import ExecResult, SandboxError, SandboxSession

T = TypeVar("T")


class DockerWorkspace:
    def __init__(self, session: SandboxSession) -> None:
        self.session = session

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        return _to_output(await self._call(self.session.exec_tool, argv, timeout_s))

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        return _to_output(await self._call(self._write, files))

    async def reap(self) -> None:
        await self._call(self.session.reap)

    def _write(self, files: dict[str, str]) -> ExecResult:
        return self.session.write_files_result(files, mtime=time.time())

    @staticmethod
    async def _call(fn: Callable[..., T], *args: object) -> T:
        try:
            return await asyncio.to_thread(fn, *args)
        except SandboxError as exc:
            raise WorkspaceUnavailable(str(exc)) from exc
        except docker.errors.DockerException as exc:
            raise WorkspaceUnavailable(f"docker error: {exc}") from exc


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
