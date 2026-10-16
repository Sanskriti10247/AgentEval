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

from agenteval_core.evaluation.results import ExecPlan, FileEvidence
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.tools.workspace import ExecOutput, WorkspaceUnavailable
from agenteval_worker.sandbox.evaluation import EvaluationSession
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

    async def abort(self) -> None:
        # SandboxSession.kill swallows Docker errors: the container may be gone.
        await asyncio.to_thread(self.session.kill)

    def _write(self, files: dict[str, str]) -> ExecResult:
        return self.session.write_files_result(files, mtime=time.time())

    @staticmethod
    async def _call(fn: Callable[..., T], *args: object) -> T:
        try:
            return await asyncio.to_thread(fn, *args)
        except SandboxError as exc:
            raise WorkspaceUnavailable(str(exc), reason=exc.reason) from exc
        except docker.errors.DockerException as exc:
            raise WorkspaceUnavailable(
                f"docker error: {exc}", reason=FailureReason.SANDBOX_EXEC_FAILED
            ) from exc


class EvaluationWorkspace:
    """Async adapter for an EvaluationSession, with DockerWorkspace's error
    contract (sandbox failures become WorkspaceUnavailable). The Step 5
    runner drives Step 3 ExecPlans and evidence collection through it."""

    def __init__(self, session: EvaluationSession) -> None:
        self.session = session

    async def run_plan(self, plan: ExecPlan) -> ExecOutput:
        return _to_output(await DockerWorkspace._call(self.session.run_plan, plan))

    async def collect_evidence(self, paths: list[str]) -> dict[str, FileEvidence]:
        return await DockerWorkspace._call(self.session.collect_evidence, paths)

    async def abort(self) -> None:
        await asyncio.to_thread(self.session.kill)


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
