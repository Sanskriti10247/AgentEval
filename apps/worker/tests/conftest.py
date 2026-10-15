"""Shared fixtures for the Phase 4 evaluation-sandbox tests (real Docker)."""

from collections.abc import Callable, Iterator

import docker
import pytest

from agenteval_core.evaluation.checks import evaluator_files
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_worker.sandbox.evaluation import (
    EvaluationSession,
    WorkspaceSnapshot,
    export_workspace,
)
from agenteval_worker.sandbox.session import ContainerSpec, SandboxSession

IMAGE = "agenteval-sandbox:py312"
EVAL_LABELS = {"agenteval.test": "eval-sandbox"}
EVAL_SPEC = ContainerSpec(IMAGE, lifetime_s=300, memory_mb=256, cpu_limit=1.0, labels=EVAL_LABELS)


@pytest.fixture(scope="session")
def docker_client() -> Iterator[docker.DockerClient]:
    c = docker.from_env()
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{IMAGE} not built; run `make sandbox-image`")
    yield c
    for leftover in c.containers.list(all=True, filters={"label": "agenteval.test=eval-sandbox"}):
        leftover.remove(force=True)
    c.close()


@pytest.fixture
def sessions() -> Iterator[list[SandboxSession]]:
    """Every session a test registers is closed afterwards."""
    opened: list[SandboxSession] = []
    yield opened
    for s in opened:
        s.close()


@pytest.fixture
def agent_snapshot(
    docker_client: docker.DockerClient, sessions: list[SandboxSession]
) -> Callable[..., tuple[SandboxSession, WorkspaceSnapshot]]:
    """Open an agent container with `files`, run `work` as the agent would,
    reap, and export the workspace."""

    def make(files: dict[str, str], work: str = "") -> tuple[SandboxSession, WorkspaceSnapshot]:
        agent = SandboxSession.open_for_tools(docker_client, EVAL_SPEC, files)
        sessions.append(agent)
        if work:
            result = agent.exec_tool(["sh", "-c", work], timeout_s=30)
            assert result.exit_code == 0, result.stderr
        agent.reap()
        return agent, export_workspace(agent)

    return make


@pytest.fixture
def eval_session(
    docker_client: docker.DockerClient, sessions: list[SandboxSession]
) -> Callable[..., EvaluationSession]:
    def make(
        snapshot: WorkspaceSnapshot,
        spec: EvaluationSpec | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> EvaluationSession:
        files: dict[str, str] = {}
        if spec is not None:
            files = {**spec.hidden_files, **evaluator_files(spec)}
        files.update(extra_files or {})
        session = EvaluationSession.open(docker_client, EVAL_SPEC, snapshot, files)
        sessions.append(session)
        return session

    return make
