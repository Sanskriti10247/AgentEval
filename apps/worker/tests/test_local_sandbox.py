"""The local sandbox backend (sandbox/local.py): no Docker, plain subprocesses. These run
anywhere the unit suite runs (Linux: they read /proc)."""

import asyncio
import io
import os
import socket
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenteval_core.evaluation.checks import evaluator_files, plan
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.states import Outcome
from agenteval_core.tools.workspace import WorkspaceUnavailable
from agenteval_worker.sandbox.docker_sandbox import SandboxSpec
from agenteval_worker.sandbox.evaluation import SnapshotError
from agenteval_worker.sandbox.local import (
    IMAGE_ID,
    LOCAL_PATH,
    MARKER_ENV,
    LocalAgentSandbox,
    LocalSandbox,
)
from agenteval_worker.sandbox.session import SandboxError

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

# The interpreter running the tests (a venv's bin also holds `python`) comes first on the
# sandbox's PATH, so the suite doesn't depend on what /usr/bin holds on the machine.
PATH = f"{Path(sys.executable).parent}:{LOCAL_PATH}"
PY = Path(sys.executable).name

HIDDEN_TESTS = (
    "import unittest\nfrom calc import add\n\n\n"
    "class Hidden(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)


@pytest.fixture
def backend(tmp_path: Path) -> LocalSandbox:
    return LocalSandbox(tmp_path, max_output_bytes=4096, workspace_mb=4, path=PATH)


@pytest.fixture
def agent(backend: LocalSandbox) -> Iterator[LocalAgentSandbox]:
    sandbox = backend.open_agent(
        {"calc.py": "def add(a, b):\n    return a - b\n", "pkg/data.txt": "x\n"},
        lifetime_s=60,
        memory_mb=256,
        attempt_id="attempt-1",
        image="agenteval-sandbox:py312",
    )
    yield sandbox
    sandbox.close()


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except OSError:
        return False


def _wait_dead(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


async def test_exec_runs_in_the_workspace_with_a_scrubbed_environment(
    agent: LocalAgentSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTEVAL_DATABASE_URL", "postgresql://secret@db/x")
    out = await agent.workspace.exec(["sh", "-c", "pwd; ls; env"], timeout_s=10)
    assert out.exit_code == 0 and not out.timed_out
    lines = out.stdout.splitlines()
    assert lines[0] == "/workspace"  # the run directory never shows in output
    assert {"calc.py", "pkg"} <= set(lines)
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    assert "AGENTEVAL_DATABASE_URL" not in env
    assert env["PATH"] == PATH
    assert env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"  # TOOL_EXEC_ENV
    assert MARKER_ENV in env


async def test_absolute_sandbox_paths_in_argv_map_to_the_run_directory(
    agent: LocalAgentSandbox,
) -> None:
    out = await agent.workspace.exec(["cat", "/workspace/pkg/data.txt"], timeout_s=10)
    assert (out.exit_code, out.stdout) == (0, "x\n")
    missing = await agent.workspace.exec(["cat", "/workspace/nope"], timeout_s=10)
    assert missing.exit_code == 1 and "/workspace/nope" in missing.stderr


async def test_a_missing_program_is_exit_127_not_an_error(agent: LocalAgentSandbox) -> None:
    out = await agent.workspace.exec(["no-such-program-xyz"], timeout_s=5)
    assert out.exit_code == 127 and "no-such-program-xyz" in out.stderr


async def test_timeout_kills_the_whole_process_group_and_the_workspace_stays_usable(
    agent: LocalAgentSandbox,
) -> None:
    started = time.monotonic()
    out = await agent.workspace.exec(
        ["sh", "-c", "sleep 30 & echo $! > bg.pid; sleep 30"], timeout_s=0.5
    )
    assert out.timed_out and out.exit_code in (124, 137)
    assert time.monotonic() - started < 5
    bg = int((agent.session.workspace / "bg.pid").read_text())
    assert _wait_dead(bg)
    again = await agent.workspace.exec(["echo", "ok"], timeout_s=5)
    assert (again.exit_code, again.stdout) == (0, "ok\n")


async def test_reap_kills_a_process_that_left_the_group_with_setsid(
    agent: LocalAgentSandbox,
) -> None:
    out = await agent.workspace.exec(
        [PY, "-c", "import subprocess; print(subprocess.Popen(['sleep', '60'], "
         "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)"],
        timeout_s=10,
    )  # fmt: skip
    escaped = int(out.stdout)
    assert _alive(escaped)
    await agent.workspace.reap()
    assert _wait_dead(escaped)


async def test_output_is_bounded(agent: LocalAgentSandbox) -> None:
    out = await agent.workspace.exec([PY, "-c", "print('x' * 100_000)"], timeout_s=10)
    assert out.exit_code == 0
    assert out.stdout_truncated and len(out.stdout) == 4096


async def test_file_size_limit_applies(agent: LocalAgentSandbox) -> None:
    # workspace_mb=4: a 5 MiB file is over RLIMIT_FSIZE.
    out = await agent.workspace.exec(
        [PY, "-c", "open('big', 'wb').write(b'0' * (5 * 1024 * 1024))"], timeout_s=10
    )
    assert out.exit_code != 0
    assert (agent.session.workspace / "big").stat().st_size <= 4 * 1024 * 1024


async def test_write_files_refuses_to_write_through_a_symlinked_directory(
    agent: LocalAgentSandbox, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (agent.session.workspace / "link").symlink_to(outside)
    out = await agent.workspace.write_files({"link/evil.txt": "x", "ok.txt": "fine"})
    assert out.exit_code != 0 and "link/evil.txt" in out.stderr
    assert not (outside / "evil.txt").exists()
    assert (agent.session.workspace / "ok.txt").read_text() == "fine"
    (agent.session.workspace / "final").symlink_to(outside / "target")
    out = await agent.workspace.write_files({"final": "x"})
    assert out.exit_code != 0 and not (outside / "target").exists()


async def test_abort_ends_an_exec_in_flight_and_the_workspace_is_unusable(
    agent: LocalAgentSandbox,
) -> None:
    running = asyncio.create_task(agent.workspace.exec(["sleep", "30"], timeout_s=60))
    await asyncio.sleep(0.3)
    await agent.workspace.abort()
    with pytest.raises(WorkspaceUnavailable):
        await asyncio.wait_for(running, 10)
    with pytest.raises(WorkspaceUnavailable):
        await agent.workspace.exec(["true"], timeout_s=5)


def test_kill_attempt_aborts_that_attempts_sessions_only(backend: LocalSandbox) -> None:
    mine = backend.open_agent({}, lifetime_s=60, memory_mb=256, attempt_id="a")
    other = backend.open_agent({}, lifetime_s=60, memory_mb=256, attempt_id="b")
    try:
        result: list[object] = []

        def run() -> None:
            try:
                result.append(mine.session.exec_tool(["sleep", "30"], 60))
            except SandboxError as exc:
                result.append(exc)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.3)
        assert backend.kill_attempt("a") == 1
        t.join(10)
        assert isinstance(result[0], SandboxError)
        assert other.session.exec_tool(["true"], 5).exit_code == 0
    finally:
        mine.close()
        other.close()
    assert backend.kill_attempt("a") == 0


def test_close_removes_the_run_directory_and_sweep_removes_expired_leftovers(
    backend: LocalSandbox,
) -> None:
    sandbox = backend.open_agent({"a.txt": "a"}, lifetime_s=60, memory_mb=256)
    base = sandbox.session.base
    assert base.is_dir()
    sandbox.close()
    assert not base.exists()

    expired = backend.root / "run-1-deadbeef"
    current = backend.root / f"run-{time.time() + 3600:.0f}-cafe"
    for d in (expired, current):
        (d / "workspace").mkdir(parents=True)
    (expired / "workspace" / "ro").mkdir(mode=0o500)  # read-only leftovers go too
    assert backend.sweep_orphans() == 1
    assert not expired.exists() and current.exists()


def test_the_task_image_is_not_used_and_recorded_as_local(agent: LocalAgentSandbox) -> None:
    assert agent.image_id() == IMAGE_ID == "local"


def test_command_tasks_success_failure_and_timeout(backend: LocalSandbox) -> None:
    def spec(command: list[str], timeout_s: int = 10) -> SandboxSpec:
        return SandboxSpec(
            image="agenteval-sandbox:py312",
            command=command,
            files={"in.txt": "hello\n"},
            timeout_s=timeout_s,
            memory_mb=256,
            cpu_limit=1.0,
            labels={"agenteval.attempt_id": "cmd"},
        )

    ok = backend.run(spec(["cat", "in.txt"]))
    assert (ok.outcome, ok.exit_code, ok.stdout) == (Outcome.SUCCESS, 0, "hello\n")
    failed = backend.run(spec(["sh", "-c", "echo bad >&2; exit 3"]))
    assert (failed.outcome, failed.exit_code, failed.stderr) == (
        Outcome.NON_ZERO_EXIT,
        3,
        "bad\n",
    )
    timed = backend.run(spec(["sleep", "30"], timeout_s=1))
    assert timed.outcome == Outcome.TIMEOUT and timed.duration_ms < 5000
    assert list(backend.root.iterdir()) == []  # every run directory is gone


def test_export_normalizes_modes_and_rejects_what_tar_cannot_store(
    agent: LocalAgentSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = agent.session.workspace
    (ws / "secret").write_text("s")
    (ws / "secret").chmod(0o200)
    snapshot = agent.export(max_bytes=1 << 20, max_unpacked_bytes=1 << 20, timeout_s=10)
    assert snapshot.normalized_paths == ("secret",)
    with tarfile.open(fileobj=io.BytesIO(snapshot.data), mode="r:gz") as tar:
        names = {m.name.removeprefix("./") for m in tar}
    assert {"calc.py", "pkg", "pkg/data.txt", "secret"} <= names

    with pytest.raises(SnapshotError) as too_large:
        agent.export(max_bytes=64, max_unpacked_bytes=1 << 20, timeout_s=10)
    assert too_large.value.reason == "too_large"

    sock = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(ws)  # a relative name: the absolute one is over the AF_UNIX limit
    try:
        sock.bind("sock")
        with pytest.raises(SnapshotError) as incomplete:
            agent.export(max_bytes=1 << 20, max_unpacked_bytes=1 << 20, timeout_s=10)
        assert incomplete.value.reason == "incomplete"
    finally:
        sock.close()


async def test_grading_runs_hidden_tests_against_the_restored_snapshot(
    backend: LocalSandbox, agent: LocalAgentSandbox
) -> None:
    await agent.workspace.write_files({"calc.py": "def add(a, b):\n    return a + b\n"})
    snapshot = agent.export(max_bytes=1 << 20, max_unpacked_bytes=1 << 20, timeout_s=10)
    spec = EvaluationSpec.model_validate(
        {
            "checks": [
                {"kind": "python_tests", "name": "hidden", "pattern": "test_h*.py",
                 "expected_tests": 1},
                {"kind": "command", "name": "cmd", "trusted_environment": True,
                 "argv": ["sh", "-c", "test -f /eval/test_hidden.py && touch dirty"]},
            ],
            "hidden_files": {"test_hidden.py": HIDDEN_TESTS},
        }
    )  # fmt: skip
    evaluation = backend.open_evaluation(
        snapshot,
        {**spec.hidden_files, **evaluator_files(spec)},
        lifetime_s=60,
        memory_mb=256,
        attempt_id="attempt-1",
    )
    try:
        ws = evaluation.workspace
        tests, command = (plan(c) for c in spec.checks)
        tests_out = await ws.run_plan(tests)
        assert tests_out.exit_code == 0, tests_out.stderr
        assert "Ran 1 test" in tests_out.stderr
        assert (await ws.run_plan(command)).exit_code == 0
        assert (evaluation.session.workspace / "dirty").exists()
        # Evidence always comes from the pristine snapshot: the plan's file is gone.
        evidence = await ws.collect_evidence(["calc.py", "dirty", "pkg"])
        assert evidence["calc.py"].content == b"def add(a, b):\n    return a + b\n"
        assert not evidence["dirty"].exists
        assert evidence["pkg"].exists and evidence["pkg"].content is None
        # /eval is read-only by mode.
        assert not os.access(evaluation.session.eval_dir / "test_hidden.py", os.W_OK)
    finally:
        evaluation.close()
    assert not evaluation.session.base.exists()


def test_a_snapshot_whose_hash_does_not_match_is_refused(
    backend: LocalSandbox, agent: LocalAgentSandbox
) -> None:
    snapshot = agent.export(max_bytes=1 << 20, max_unpacked_bytes=1 << 20, timeout_s=10)
    forged = type(snapshot)(snapshot.data, "0" * 64)
    with pytest.raises(SnapshotError):
        backend.open_evaluation(forged, {}, lifetime_s=60, memory_mb=256)
