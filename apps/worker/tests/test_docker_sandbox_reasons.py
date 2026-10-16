"""DockerSandbox's exception -> failure_reason mapping (Step 3), by exception
type only, never by message. No real Docker: SandboxSession.create/start are
monkeypatched so these run without a daemon."""

from typing import Any
from unittest.mock import MagicMock

import docker.errors
import pytest

from agenteval_core.reliability.taxonomy import FailureReason
from agenteval_core.states import Outcome
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox, SandboxSpec
from agenteval_worker.sandbox.session import SandboxError, SandboxSession


def spec() -> SandboxSpec:
    return SandboxSpec(
        image="img", command=["true"], files={}, timeout_s=5, memory_mb=128, cpu_limit=1.0
    )


def test_image_not_found_at_create(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_create(*a: Any, **kw: Any) -> SandboxSession:
        raise docker.errors.ImageNotFound("no such image", explanation="nope")

    monkeypatch.setattr(SandboxSession, "create", staticmethod(broken_create))
    result = DockerSandbox(MagicMock()).run(spec())
    assert result.outcome is Outcome.SANDBOX_ERROR
    assert result.failure_reason is FailureReason.SANDBOX_IMAGE_NOT_FOUND


def test_other_docker_error_at_create_is_create_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_create(*a: Any, **kw: Any) -> SandboxSession:
        raise docker.errors.APIError("daemon unreachable")

    monkeypatch.setattr(SandboxSession, "create", staticmethod(broken_create))
    result = DockerSandbox(MagicMock()).run(spec())
    assert result.outcome is Outcome.SANDBOX_ERROR
    assert result.failure_reason is FailureReason.SANDBOX_CREATE_FAILED


def test_a_sandbox_error_keeps_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def start(self) -> None:
            raise SandboxError("reaper failed: boom", reason=FailureReason.SANDBOX_REAPER_FAILED)

        def close(self) -> None:
            pass

    monkeypatch.setattr(SandboxSession, "create", staticmethod(lambda *a, **kw: FakeSession()))
    result = DockerSandbox(MagicMock()).run(spec())
    assert result.outcome is Outcome.SANDBOX_ERROR
    assert result.failure_reason is FailureReason.SANDBOX_REAPER_FAILED


def test_exit_code_none_is_an_exec_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform anomaly (Docker never reported an exit code): sandbox_exec_failed."""

    class Result:
        timed_out = False
        exit_code = None
        stdout = ""
        stderr = ""
        stdout_truncated = False
        stderr_truncated = False
        duration_ms = 1

    class FakeSession:
        def start(self) -> None:
            pass

        def write_files(self, files: dict[str, str]) -> None:
            pass

        def exec_until_deadline(self, command: list[str], timeout_s: float) -> Any:
            return Result()

        def oom_killed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(SandboxSession, "create", staticmethod(lambda *a, **kw: FakeSession()))
    result = DockerSandbox(MagicMock()).run(spec())
    assert result.outcome is Outcome.SANDBOX_ERROR
    assert result.failure_reason is FailureReason.SANDBOX_EXEC_FAILED


def test_success_carries_no_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        timed_out = False
        exit_code = 0
        stdout = "hi"
        stderr = ""
        stdout_truncated = False
        stderr_truncated = False
        duration_ms = 1

    class FakeSession:
        def start(self) -> None:
            pass

        def write_files(self, files: dict[str, str]) -> None:
            pass

        def exec_until_deadline(self, command: list[str], timeout_s: float) -> Any:
            return Result()

        def oom_killed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(SandboxSession, "create", staticmethod(lambda *a, **kw: FakeSession()))
    result = DockerSandbox(MagicMock()).run(spec())
    assert result.outcome is Outcome.SUCCESS
    assert result.failure_reason is None
