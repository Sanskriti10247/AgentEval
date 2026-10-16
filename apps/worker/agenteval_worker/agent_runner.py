"""AgentRunner: one agent attempt, from agent version to finish_attempt fields.

    validate config (provider enabled, model priced, limits, tools)
    open a SandboxSession (task files copied, keepalive recorded)
    start a model session (system prompt + tool specs + task instructions)
    run the AgentRuntime with a Postgres TrajectoryWriter
    Phase 4: if the attempt COMPLETED and its evaluation spec needs the
        workspace, reap and export /workspace (C1) before closing
    always close the sandbox

The deadline is fixed when the runner starts, so sandbox setup counts
against the task's timeout like everything else.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import docker
import docker.errors
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agenteval_core.agent.context import AgentContext, CancelToken
from agenteval_core.agent.events import SinkFailed
from agenteval_core.agent.limits import AgentLimits
from agenteval_core.agent.pricing import ModelPrice, price_for
from agenteval_core.agent.prompts import build_system_prompt
from agenteval_core.agent.result import AgentResult
from agenteval_core.agent.retry import RetryPolicy
from agenteval_core.agent.runtime import AgentRuntime
from agenteval_core.evaluation.taxonomy import MAX_SNAPSHOT_BYTES
from agenteval_core.logging import get_logger
from agenteval_core.models import AgentVersion, RunAttempt, Task
from agenteval_core.providers.base import ModelProvider
from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import COMPLETED_OUTCOMES, FAILED_OUTCOMES, Outcome
from agenteval_core.tools.base import ToolContext
from agenteval_core.tools.registry import ToolSet
from agenteval_worker.evaluation_runner import WorkspaceCapture, needs_workspace, parse_spec
from agenteval_worker.sandbox.evaluation import WorkspaceSnapshot, export_workspace
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
    # Not a finish_attempt field: handed to the EvaluationRunner after the
    # attempt is durable (the executor excludes it when persisting).
    workspace: WorkspaceCapture | None = field(default=None, repr=False, compare=False)


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
        docker_client: docker.DockerClient,
        providers: Mapping[str, ModelProvider],
        max_output_bytes: int = 64 * 1024,
        workspace_tmpfs_mb: int = 64,
        pids_limit: int = 128,
        retry_policy: RetryPolicy | None = None,
        snapshot_max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        self._sm = sessionmaker
        self._docker = docker_client
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

        try:
            session = await asyncio.to_thread(
                SandboxSession.open_for_tools,
                self._docker,
                ContainerSpec(
                    image=task.image,
                    lifetime_s=task.timeout_s,
                    memory_mb=task.memory_mb,
                    cpu_limit=task.cpu_limit,
                    labels={
                        "agenteval.run_id": str(attempt.run_id),
                        "agenteval.attempt_id": str(attempt.id),
                    },
                ),
                dict(task.files),
                max_output_bytes=self._max_output,
                workspace_tmpfs_mb=self._workspace_mb,
                pids_limit=self._pids_limit,
            )
        except SandboxError as exc:
            return _failed(Outcome.SANDBOX_ERROR, str(exc), started, reason=exc.reason)

        image_id = await asyncio.to_thread(_image_id, session)
        try:
            assert task.instructions is not None  # ck_tasks_instructions
            model_session = setup.provider.start(
                model=version.model,
                system=build_system_prompt(version.system_prompt),
                tools=setup.toolset.specs(),
                task_prompt=task.instructions,
                settings=dict(version.model_settings),
            )
            ctx = AgentContext(
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                attempt_no=attempt.attempt_no,
                limits=setup.limits,
                deadline=deadline,
                tools=ToolContext(
                    workspace=DockerWorkspace(session),
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
            if result.outcome in COMPLETED_OUTCOMES and self._wants_workspace(task):
                result = replace(result, workspace=await self._capture(session))
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

    @staticmethod
    def _wants_workspace(task: Task) -> bool:
        """A6. An invalid spec captures nothing: the evaluator records it as
        an ERROR without needing the workspace."""
        try:
            spec = parse_spec(task.evaluation)
        except ValidationError:
            return False
        return spec is not None and needs_workspace(spec)

    async def _capture(self, session: SandboxSession) -> WorkspaceCapture:
        """C1: a complete snapshot, or the reason there isn't one (A2: an
        evaluator failure, never the agent's). Never changes the outcome."""
        try:
            snapshot = await asyncio.to_thread(self._export, session)
        except (SandboxError, docker.errors.DockerException) as exc:
            log.warning("workspace_export_failed", error_type=type(exc).__name__)
            return WorkspaceCapture(error=str(exc))
        return WorkspaceCapture(snapshot=snapshot)

    def _export(self, session: SandboxSession) -> WorkspaceSnapshot:
        session.reap()  # nothing the agent started may still be writing
        return export_workspace(
            session,
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


def _image_id(session: SandboxSession) -> str | None:
    try:
        return session.image_id
    except docker.errors.DockerException:
        return None  # reproducibility metadata only; never fails the attempt


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
