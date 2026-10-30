"""AgentRunner: one agent attempt, from agent version to finish_attempt fields.

    validate config (provider enabled, model priced, limits, tools)
    check the evaluation's spending cap (max_cost_usd), if the run has one
    open the sandbox: a SandboxSession (task files copied, keepalive recorded),
        or a LocalSandbox session when the worker runs without Docker
    start a model session (system prompt + tool specs + task instructions)
    run the AgentRuntime with a Postgres TrajectoryWriter
    Phase 4: if the attempt COMPLETED and its evaluation spec needs the
        workspace, reap and export /workspace (C1) before closing
    Step 6 (O3d): the runner only exports. The executor saves the snapshot
        (T-artifact) while the attempt is still RUNNING and passes the resulting
        `workspace_capture` to T-finish; see `AgentAttemptResult`.
    always close the sandbox

The deadline is fixed when the runner starts, so sandbox setup counts
against the task's timeout like everything else.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Protocol

import docker
import docker.errors
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.context import AgentContext, CancelToken
from agenteval_core.agent.events import SinkFailed
from agenteval_core.agent.limits import AgentLimits, evaluation_budget_exhausted
from agenteval_core.agent.pricing import ModelPrice, price_for
from agenteval_core.agent.prompts import build_system_prompt
from agenteval_core.agent.result import AgentResult
from agenteval_core.agent.retry import RetryPolicy
from agenteval_core.agent.runtime import AgentRuntime
from agenteval_core.evaluation.taxonomy import MAX_SNAPSHOT_BYTES
from agenteval_core.logging import get_logger
from agenteval_core.models import AgentVersion, RunAttempt, Task
from agenteval_core.providers.base import ModelProvider
from agenteval_core.reliability.taxonomy import FailureReason, WorkspaceCapture
from agenteval_core.repositories.batches import BatchRepository
from agenteval_core.states import COMPLETED_OUTCOMES, FAILED_OUTCOMES, Outcome
from agenteval_core.tools.base import ToolContext
from agenteval_core.tools.registry import ToolSet
from agenteval_core.tools.workspace import Workspace
from agenteval_worker.evaluation_runner import needs_workspace, parse_spec
from agenteval_worker.sandbox.evaluation import SnapshotError, WorkspaceSnapshot, export_workspace
from agenteval_worker.sandbox.local import LocalSandbox
from agenteval_worker.sandbox.session import ContainerSpec, SandboxError, SandboxSession
from agenteval_worker.sandbox.workspace import DockerWorkspace
from agenteval_worker.trajectory import TrajectoryWriter

log = get_logger(__name__)

# Per export phase (normalise, tar). A TIMEOUT attempt exports after its
# deadline, so this has to fit inside the executor's SANDBOX_GRACE_S backstop.
EXPORT_TIMEOUT_S = 20


@dataclass(frozen=True)
class AgentAttemptResult:
    """Same fields as ExecutionResult (so Executor._persist treats both
    alike) plus the agent aggregates finish_attempt stores."""

    outcome: Outcome
    duration_ms: int
    error_message: str | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    steps: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: Decimal | None = None
    final_answer: str | None = None
    sandbox_image_id: str | None = None
    # Set exactly for a failed outcome; never inferred from error_message.
    failure_reason: FailureReason | None = None
    # O3d: what became of the workspace snapshot - set only for a completed
    # outcome of an agent task (ck_run_attempts_capture), a finish_attempt field.
    # While `snapshot` is set this stays None: the executor saves the snapshot
    # and fills in stored / persist_failed.
    workspace_capture: WorkspaceCapture | None = None
    # Not a finish_attempt field: the exported bytes, waiting to be saved by the
    # executor (which clears it, and excludes it when persisting).
    snapshot: WorkspaceSnapshot | None = field(default=None, repr=False, compare=False)


class _AgentSandbox(Protocol):
    """An agent attempt's sandbox, whichever backend opened it. Blocking methods:
    callers use asyncio.to_thread."""

    @property
    def workspace(self) -> Workspace: ...

    def image_id(self) -> str | None: ...

    def export(
        self, *, max_bytes: int, max_unpacked_bytes: int, timeout_s: float
    ) -> WorkspaceSnapshot:
        """Reap, then the C1 snapshot of the workspace (or a SnapshotError)."""
        ...

    def close(self) -> None: ...


class _DockerAgentSandbox:
    def __init__(self, session: SandboxSession) -> None:
        self.session = session
        self._workspace = DockerWorkspace(session)

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    def image_id(self) -> str | None:
        try:
            return self.session.image_id
        except docker.errors.DockerException:
            return None  # reproducibility metadata only; never fails the attempt

    def export(
        self, *, max_bytes: int, max_unpacked_bytes: int, timeout_s: float
    ) -> WorkspaceSnapshot:
        self.session.reap()  # nothing the agent started may still be writing
        return export_workspace(
            self.session,
            max_bytes=max_bytes,
            max_unpacked_bytes=max_unpacked_bytes,
            timeout_s=timeout_s,
        )

    def close(self) -> None:
        self.session.close()


class _SetupError(Exception):
    def __init__(self, message: str, *, reason: FailureReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _Setup:
    provider: ModelProvider
    price: ModelPrice
    limits: AgentLimits
    toolset: ToolSet


class AgentRunner:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        docker_client: docker.DockerClient | None,
        providers: Mapping[str, ModelProvider],
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
        retry_policy: RetryPolicy | None = None,
        snapshot_max_bytes: int = MAX_SNAPSHOT_BYTES,
        image_allowlist: Iterable[str] | None = None,
        local_sandbox: LocalSandbox | None = None,
    ) -> None:
        if (docker_client is None) == (local_sandbox is None):
            raise ValueError("pass exactly one of docker_client and local_sandbox")
        self._allowlist = None if image_allowlist is None else frozenset(image_allowlist)
        self._sm = sessionmaker
        self._docker = docker_client
        self._local = local_sandbox
        self._providers = dict(providers)
        self._max_output = max_output_bytes
        self._workspace_mb = workspace_tmpfs_mb
        self._pids_limit = pids_limit
        self._retry = retry_policy
        self._snapshot_max = snapshot_max_bytes

    async def run(
        self, task: Task, attempt: RunAttempt, version: AgentVersion, cancel: CancelToken
    ) -> AgentAttemptResult:
        started = time.monotonic()
        deadline = started + task.timeout_s
        try:
            setup = self._prepare(task, version)
        except _SetupError as exc:
            return _failed(Outcome.INTERNAL_ERROR, str(exc), started, reason=exc.reason)
        exhausted = await self._budget_exhausted(attempt)
        if exhausted is not None:
            log.warning("evaluation_budget_exhausted", detail=exhausted)
            return _failed(
                Outcome.INTERNAL_ERROR,
                exhausted,
                started,
                reason=FailureReason.EVALUATION_BUDGET_EXHAUSTED,
            )

        try:
            session = await asyncio.to_thread(self._open, task, attempt)
        except SandboxError as exc:
            return _failed(Outcome.SANDBOX_ERROR, str(exc), started, reason=exc.reason)

        image_id = await asyncio.to_thread(session.image_id)
        try:
            assert task.instructions is not None  # ck_tasks_instructions
            model_session = setup.provider.start(
                model=version.model,
                system=build_system_prompt(version.system_prompt),
                tools=setup.toolset.specs(),
                task_prompt=task.instructions,
                settings=dict(version.model_settings),
                attempt_no=attempt.attempt_no,
            )
            ctx = AgentContext(
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                attempt_no=attempt.attempt_no,
                limits=setup.limits,
                deadline=deadline,
                tools=ToolContext(
                    workspace=session.workspace,
                    deadline=deadline,
                    tool_timeout_s=setup.limits.tool_timeout_s,
                    shell_allowlist=frozenset(version.shell_allowlist),
                    test_command=tuple(task.test_command) if task.test_command else None,
                ),
                cancel=cancel,
                config=_config_snapshot(version),
            )
            runtime = AgentRuntime(
                session=model_session,
                toolset=setup.toolset,
                price=setup.price,
                sink=TrajectoryWriter(self._sm, attempt),
                retry_policy=self._retry,
            )
            result = _from_agent_result(await runtime.run(ctx), image_id)
            if result.outcome in COMPLETED_OUTCOMES:
                if not self._wants_workspace(task):
                    result = replace(result, workspace_capture=WorkspaceCapture.NOT_NEEDED)
                else:
                    capture, snapshot = await self._capture(session)
                    result = replace(result, workspace_capture=capture, snapshot=snapshot)
            return result
        except SinkFailed as exc:
            log.error("trajectory_unrecordable", error=str(exc))
            return _failed(
                Outcome.INTERNAL_ERROR,
                str(exc),
                started,
                image_id,
                reason=FailureReason.PERSISTENCE_TRAJECTORY,
            )
        except Exception as exc:
            log.exception("agent_runner_error")
            return _failed(
                Outcome.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                started,
                image_id,
                reason=FailureReason.INTERNAL_UNEXPECTED,
            )
        finally:
            # Shielded: the container must go even if this task is being cancelled.
            await asyncio.shield(asyncio.to_thread(session.close))

    async def _budget_exhausted(self, attempt: RunAttempt) -> str | None:
        """Why the run must not start its agent loop, or None. A run of an evaluation
        with max_cost_usd starts only while the cost of all of that evaluation's
        attempts so far is below it; a run already past this point is bounded by its
        version's own per-attempt cap, so concurrent runs can overshoot the evaluation
        cap by at most what they spend."""
        async with self._sm() as s:
            spend = await BatchRepository(s).evaluation_spend(attempt.run_id)
        if spend is None:
            return None
        cap, spent = spend
        if not evaluation_budget_exhausted(spent, cap):
            return None
        return f"evaluation budget exhausted: {spent} USD spent of max_cost_usd {cap}"

    @staticmethod
    def _wants_workspace(task: Task) -> bool:
        """A6. An invalid spec captures nothing: the evaluator records it as
        an ERROR without needing the workspace."""
        try:
            spec = parse_spec(task.evaluation)
        except ValidationError:
            return False
        return spec is not None and needs_workspace(spec)

    def _open(self, task: Task, attempt: RunAttempt) -> _AgentSandbox:
        """Open the attempt's sandbox with the task files in place. Raises SandboxError."""
        labels = {"agenteval.run_id": str(attempt.run_id), "agenteval.attempt_id": str(attempt.id)}
        if self._local is not None:
            # The task's image is not used here (local.py); the attempt records "local".
            return self._local.open_agent(
                dict(task.files),
                lifetime_s=task.timeout_s,
                memory_mb=task.memory_mb,
                attempt_id=labels["agenteval.attempt_id"],
                image=task.image,
            )
        assert self._docker is not None
        return _DockerAgentSandbox(
            SandboxSession.open_for_tools(
                self._docker,
                ContainerSpec(
                    image=task.image,
                    lifetime_s=task.timeout_s,
                    memory_mb=task.memory_mb,
                    cpu_limit=task.cpu_limit,
                    labels=labels,
                    image_allowlist=self._allowlist,
                ),
                dict(task.files),
                max_output_bytes=self._max_output,
                workspace_tmpfs_mb=self._workspace_mb,
                pids_limit=self._pids_limit,
            )
        )

    async def _capture(
        self, session: _AgentSandbox
    ) -> tuple[WorkspaceCapture | None, WorkspaceSnapshot | None]:
        """C1: a complete snapshot (capture None: still to be saved), or the
        reason there isn't one (A2: an evaluator failure, never the agent's):
        the SnapshotError's too_large / incomplete / invalid, or incomplete for
        any other export failure. Never changes the outcome."""
        try:
            snapshot = await asyncio.to_thread(self._export, session)
        except (SandboxError, docker.errors.DockerException) as exc:
            log.warning("workspace_export_failed", error_type=type(exc).__name__)
            kind = exc.reason if isinstance(exc, SnapshotError) else "incomplete"
            return WorkspaceCapture(kind), None
        return None, snapshot

    def _export(self, session: _AgentSandbox) -> WorkspaceSnapshot:
        return session.export(
            max_bytes=self._snapshot_max,
            max_unpacked_bytes=self._workspace_mb * 1024 * 1024,
            timeout_s=EXPORT_TIMEOUT_S,
        )

    def _prepare(self, task: Task, version: AgentVersion) -> _Setup:
        provider = self._providers.get(version.model_provider)
        if provider is None:
            raise _SetupError(
                f"model provider '{version.model_provider}' is not enabled",
                reason=FailureReason.CONFIG_PROVIDER_DISABLED,
            )
        price = price_for(version.model_provider, version.model)
        if price is None:
            raise _SetupError(
                f"no price for {version.model_provider}/{version.model}",
                reason=FailureReason.CONFIG_MODEL_UNPRICED,
            )
        try:
            limits = AgentLimits.model_validate(version.limits)
            toolset = ToolSet.build(list(version.tools), has_test_command=bool(task.test_command))
        except (ValidationError, ValueError) as exc:
            raise _SetupError(
                f"invalid agent version config: {exc}", reason=FailureReason.CONFIG_INVALID
            ) from exc
        return _Setup(provider, price, limits, toolset)


def _config_snapshot(version: AgentVersion) -> dict[str, Any]:
    """Identity of the exact config. Versions are immutable, so the hash and
    id are enough to recover every field."""
    return {
        "agent_id": str(version.agent_id),
        "agent_version_id": str(version.id),
        "version": version.version,
        "model_provider": version.model_provider,
        "model": version.model,
        "config_hash": version.config_hash,
    }


def _from_agent_result(r: AgentResult, image_id: str | None) -> AgentAttemptResult:
    return AgentAttemptResult(
        outcome=r.outcome,
        duration_ms=r.duration_ms,
        # Why a FAILED run failed; COMPLETED reasons live in run_finished.
        error_message=r.reason if r.outcome in FAILED_OUTCOMES else None,
        steps=r.steps,
        tokens_in=r.usage.total_input_tokens,
        tokens_out=r.usage.output_tokens,
        cost_usd=r.cost_usd,
        final_answer=r.final_answer,
        sandbox_image_id=image_id,
        failure_reason=r.failure_reason if r.outcome in FAILED_OUTCOMES else None,
    )


def _failed(
    outcome: Outcome,
    message: str,
    started: float,
    image_id: str | None = None,
    *,
    reason: FailureReason,
) -> AgentAttemptResult:
    return AgentAttemptResult(
        outcome=outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        error_message=message,
        sandbox_image_id=image_id,
        failure_reason=reason,
    )
