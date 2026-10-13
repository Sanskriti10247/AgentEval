"""Evaluation-container support (Phase 4 step 4), functional tests on real Docker.

The Step 3 domain drives the sandbox here: plans come from checks.plan() and
are judged by checks.judge(), exactly as the Step 5 runner will do it.
"""

import io
import tarfile
import time
from collections.abc import Callable
from typing import Any

import docker
import pytest

from agenteval_core.evaluation.checks import judge, plan
from agenteval_core.evaluation.results import ExecPlan
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_core.evaluation.taxonomy import MAX_SNAPSHOT_BYTES
from agenteval_core.hashing import sha256_hex
from agenteval_core.tools.workspace import WorkspaceUnavailable
from agenteval_worker.sandbox.docker_sandbox import DockerSandbox, SandboxSpec
from agenteval_worker.sandbox.evaluation import (
    EVALUATION_ROLE_LABEL,
    EvaluationSession,
    SnapshotError,
    WorkspaceSnapshot,
    export_workspace,
    validate_snapshot,
)
from agenteval_worker.sandbox.session import DEADLINE_LABEL, MANAGED_LABEL, SandboxSession
from agenteval_worker.sandbox.workspace import EvaluationWorkspace, _to_output

pytestmark = pytest.mark.docker

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
HIDDEN_TEST = (
    "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"
)


def spec_with_tests(**kw: Any) -> EvaluationSpec:
    return EvaluationSpec.model_validate(
        {
            "checks": [{"kind": "python_tests", "name": "hidden", "expected_tests": 2}],
            "hidden_files": {"test_hidden.py": HIDDEN_TEST},
            **kw,
        }
    )


def sh(session: SandboxSession, script: str) -> str:
    return session.exec_tool(["sh", "-c", script], timeout_s=20).stdout


# -- C8: real mtimes for agent-task files --------------------------------------------------


def test_agent_task_files_get_real_mtimes(
    docker_client: docker.DockerClient, sessions: list[SandboxSession]
) -> None:
    agent = SandboxSession.open_for_tools(docker_client, _spec(), {"calc.py": BUGGY})
    sessions.append(agent)
    age = int(sh(agent, "echo $(( $(date +%s) - $(stat -c %Y calc.py) ))").strip())
    assert 0 <= age < 120


def test_s1_command_task_files_keep_mtime_zero(docker_client: docker.DockerClient) -> None:
    """S1 execution is unchanged by C8."""
    result = DockerSandbox(docker_client).run(
        SandboxSpec(
            image="agenteval-sandbox:py312",
            command=["stat", "-c", "%Y", "main.py"],
            files={"main.py": "x"},
            timeout_s=20,
            memory_mb=128,
            cpu_limit=1.0,
            labels={"agenteval.test": "eval-sandbox"},
        )
    )
    assert result.stdout.strip() == "0"


def _spec() -> Any:
    from agenteval_worker.sandbox.session import ContainerSpec

    return ContainerSpec(
        "agenteval-sandbox:py312", 300, 256, 1.0, {"agenteval.test": "eval-sandbox"}
    )


# -- export ---------------------------------------------------------------------------------------

MANIFEST = (
    "cd /workspace && find . -mindepth 1 -printf '%y %m %s %P -> %l\\n' | LC_ALL=C sort && "
    "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum"
)


def test_export_import_round_trip(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    work = (
        "mkdir -p pkg/empty && printf 'naïve ✓' > pkg/u.txt && printf '#!/bin/sh\\n' > run.sh "
        "&& chmod 755 run.sh && ln -s pkg/u.txt rel && ln -s /etc/hostname abs && mkfifo pipe"
    )
    agent, snap = agent_snapshot({"calc.py": BUGGY}, work)
    assert snap.sha256 == sha256_hex(snap.data) and snap.normalized_paths == ()
    session = eval_session(snap)
    assert sh(agent, MANIFEST) == sh(session, MANIFEST)


def test_snapshot_cap(
    agent_snapshot: Callable[..., Any],
    docker_client: docker.DockerClient,
    sessions: list[SandboxSession],
) -> None:
    agent = SandboxSession.open_for_tools(docker_client, _spec(), {})
    sessions.append(agent)
    sh(agent, "head -c 2000000 /dev/urandom > big.bin")
    with pytest.raises(SnapshotError) as info:
        export_workspace(agent, max_bytes=1_000_000)
    assert info.value.reason == "too_large"
    assert export_workspace(agent, max_bytes=MAX_SNAPSHOT_BYTES).size > 1_000_000


def test_export_fails_loudly_when_tar_fails(
    docker_client: docker.DockerClient,
    sessions: list[SandboxSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SandboxSession.open_for_tools(docker_client, _spec(), {"a.txt": "x"})
    sessions.append(agent)
    real_run = SandboxSession._run

    def failing_tar(self: SandboxSession, argv: list[str], **kw: Any) -> Any:
        if argv[0] == "tar":
            argv = ["sh", "-c", "echo 'tar: ./a.txt: Cannot open' >&2; exit 2"]
        return real_run(self, argv, **kw)

    monkeypatch.setattr(SandboxSession, "_run", failing_tar)
    with pytest.raises(SnapshotError) as info:
        export_workspace(agent)
    assert info.value.reason == "incomplete" and "Cannot open" in str(info.value)


# -- archive validation (host-side, headers only) -------------------------------------------------


def archive(*members: tuple[str, str, Any], gz: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as t:
        for kind, name, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode()
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
                continue
            ti.type = {
                "dir": tarfile.DIRTYPE,
                "sym": tarfile.SYMTYPE,
                "lnk": tarfile.LNKTYPE,
                "chr": tarfile.CHRTYPE,
                "blk": tarfile.BLKTYPE,
                "unknown": b"Z",
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            if kind in ("sym", "lnk"):
                ti.linkname = payload
            t.addfile(ti)
    return buf.getvalue()


def test_valid_archives_pass() -> None:
    validate_snapshot(
        archive(
            ("dir", "./pkg", None),
            ("file", "./pkg/a.py", "x"),
            ("sym", "./abs", "/etc/hostname"),
            ("lnk", "./b", "pkg/a.py"),
            ("fifo", "./pipe", None),
        ),
        max_unpacked_bytes=1000,
    )


@pytest.mark.parametrize(
    ("members", "fragment"),
    [
        ((("file", "/etc/x", "x"),), "unsafe member name"),
        ((("file", "../../tmp/x", "x"),), "unsafe member name"),
        ((("file", "./a/../../x", "x"),), "unsafe member name"),
        ((("lnk", "./h", "/eval/runner.py"),), "escapes the archive"),
        ((("lnk", "./h", "not-in-archive"),), "escapes the archive"),
        ((("chr", "./dev", None),), "device node"),
        ((("blk", "./disk", None),), "device node"),
        ((("unknown", "./weird", None),), "unsupported member type"),
        ((("file", "./a", "x" * 600), ("file", "./b", "x" * 600)), "unpacks to more than"),
    ],
)
def test_unsafe_archives_are_rejected(
    members: tuple[tuple[str, str, Any], ...], fragment: str
) -> None:
    with pytest.raises(SnapshotError) as info:
        validate_snapshot(archive(*members), max_unpacked_bytes=1000)
    assert info.value.reason == "invalid" and fragment in str(info.value)


def test_member_count_and_corruption_limits() -> None:
    many = archive(*[("file", f"./f{i}", "") for i in range(30)])
    with pytest.raises(SnapshotError, match="more than 10 members"):
        validate_snapshot(many, max_unpacked_bytes=1000, max_members=10)
    with pytest.raises(SnapshotError, match="unreadable archive"):
        validate_snapshot(b"\x1f\x8bnot really gzip", max_unpacked_bytes=1000)


def test_tampered_or_unsafe_snapshots_never_create_a_container(
    docker_client: docker.DockerClient,
) -> None:
    def count() -> int:
        return len(
            docker_client.containers.list(
                all=True, filters={"label": "agenteval.test=eval-sandbox"}
            )
        )

    before = count()
    unsafe = archive(("file", "../x", "x"))
    snapshots = [
        WorkspaceSnapshot(archive(("file", "./a", "x")), "0" * 64),  # digest mismatch
        WorkspaceSnapshot(unsafe, sha256_hex(unsafe)),  # traversal member
    ]
    for snap in snapshots:
        with pytest.raises(SnapshotError):
            EvaluationSession.open(docker_client, _spec(), snap, {})
    assert count() == before


# -- the evaluation container ---------------------------------------------------------------------


def test_container_layout_labels_and_image(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": FIXED})
    session = eval_session(snap, spec_with_tests())
    listing = sh(session, "ls -A /eval /eval/.agenteval; ls -A /workspace; ls -A /tmp")
    assert "test_hidden.py" in listing and "run_unittest.py" in listing and "calc.py" in listing
    assert "test_hidden.py" not in sh(session, "ls -A /workspace")
    labels = session.container.labels
    assert labels[EVALUATION_ROLE_LABEL] == "evaluation" and MANAGED_LABEL in labels
    assert DEADLINE_LABEL in labels
    assert (session.image_id or "").startswith("sha256:")


@pytest.mark.parametrize(("solution", "passed"), [(FIXED, True), (BUGGY, False)])
def test_hidden_tests_run_through_the_step3_plan(
    agent_snapshot: Callable[..., Any],
    eval_session: Callable[..., EvaluationSession],
    solution: str,
    passed: bool,
) -> None:
    _, snap = agent_snapshot({"calc.py": solution})
    spec = spec_with_tests()
    session = eval_session(snap, spec)
    result = judge(spec.checks[0], _to_output(session.run_plan(plan(spec.checks[0]))))
    assert result.passed is passed
    assert (result.tests_total, result.tests_passed) == (2, 2 if passed else 1)


def test_pytest_checks_fail_closed_when_pytest_is_missing(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    """The production image has no pytest: the check must fail, never pass."""
    _, snap = agent_snapshot({"calc.py": FIXED})
    spec = spec_with_tests(
        checks=[{"kind": "python_tests", "name": "p", "framework": "pytest", "expected_tests": 2}]
    )
    session = eval_session(snap, spec)
    result = judge(spec.checks[0], _to_output(session.run_plan(plan(spec.checks[0]))))
    assert result.passed is False and "no test summary" in (result.actual or "")


def test_io_check_gets_stdin_and_the_plan_env(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    solve = (
        "import os, sys\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n"
        "print(os.environ.get('PYTHONPYCACHEPREFIX'), file=sys.stderr)\n"
    )
    _, snap = agent_snapshot({"solve.py": solve})
    spec = EvaluationSpec.model_validate(
        {
            "checks": [
                {
                    "kind": "io",
                    "name": "io",
                    "argv": ["python", "solve.py"],
                    "stdin": "2 3",
                    "expected_stdout": "5",
                }
            ]
        }
    )
    session = eval_session(snap, spec)
    out = _to_output(session.run_plan(plan(spec.checks[0])))
    assert out.stderr.strip() == "/eval/.nopyc"
    assert judge(spec.checks[0], out).passed is True


def test_large_stdin_with_early_output_does_not_deadlock(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    chatty = (
        "import sys\nsys.stdout.write('x' * 500000)\nsys.stdout.flush()\n"
        "print(len(sys.stdin.read()))\n"
    )
    _, snap = agent_snapshot({"chatty.py": chatty})
    session = eval_session(snap)
    t0 = time.monotonic()
    result = session.run_plan(ExecPlan("c", ["python", "chatty.py"], 30, stdin="y" * 64_000))
    assert result.exit_code == 0 and time.monotonic() - t0 < 20
    assert result.stdout_truncated  # 64 KiB stream cap, still drained to the end


def test_command_check(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"check.sh": "test -f calc.py\n", "calc.py": FIXED})
    spec = EvaluationSpec.model_validate(
        {
            "checks": [
                {
                    "kind": "command",
                    "name": "c",
                    "argv": ["sh", "check.sh"],
                    "trusted_environment": True,
                }
            ]
        }
    )
    session = eval_session(snap, spec)
    assert judge(spec.checks[0], _to_output(session.run_plan(plan(spec.checks[0])))).passed


def test_fresh_workspace_restores_snapshot_and_wipes_tmp(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap)
    tamper = "printf 'x' > calc.py && touch /tmp/planted new_file && chmod 000 calc.py"
    session.run_plan(ExecPlan("a", ["sh", "-c", tamper], 10))
    kept = session.run_plan(
        ExecPlan("b", ["sh", "-c", "ls -A /tmp; ls -A"], 10, fresh_workspace=False)
    )
    assert "planted" in kept.stdout and "new_file" in kept.stdout
    fresh = session.run_plan(ExecPlan("c", ["sh", "-c", "ls -A /tmp; cat calc.py"], 10))
    assert "planted" not in fresh.stdout and fresh.stdout.strip() == BUGGY.strip()


def test_evidence_is_taken_from_the_pristine_snapshot(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    work = (
        "printf 'hello' > out.txt && mkdir d && ln -s out.txt link "
        "&& ln -s /eval/test_hidden.py peek && ln -s /eval evaldir "
        "&& head -c 100000 /dev/zero > big.bin"
    )
    _, snap = agent_snapshot({"calc.py": FIXED}, work)
    session = eval_session(snap, spec_with_tests())
    session.run_plan(ExecPlan("tamper", ["sh", "-c", "printf 'forged' > out.txt"], 10))
    ev = session.collect_evidence(
        ["out.txt", "missing.txt", "link", "peek", "evaldir/test_hidden.py", "d", "big.bin"]
    )
    assert ev["out.txt"].content == b"hello" and ev["out.txt"].sha256 == sha256_hex(b"hello")
    assert ev["missing.txt"].exists is False
    for symlinked in ("link", "peek", "evaldir/test_hidden.py"):
        assert ev[symlinked].is_symlink and ev[symlinked].content is None  # never read through
    assert ev["d"].exists and ev["d"].sha256 is None
    assert ev["big.bin"].content is None and ev["big.bin"].size == 100000


def test_close_removes_the_container(
    agent_snapshot: Callable[..., Any],
    eval_session: Callable[..., EvaluationSession],
    docker_client: docker.DockerClient,
) -> None:
    _, snap = agent_snapshot({"a.txt": "x"})
    session = eval_session(snap)
    session.close()
    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(session.container.id)


async def test_async_adapter(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": FIXED})
    spec = spec_with_tests()
    workspace = EvaluationWorkspace(eval_session(snap, spec))
    out = await workspace.run_plan(plan(spec.checks[0]))
    assert judge(spec.checks[0], out).passed is True
    assert (await workspace.collect_evidence(["calc.py"]))["calc.py"].content == FIXED.encode()
    await workspace.abort()
    with pytest.raises(WorkspaceUnavailable):
        await workspace.run_plan(plan(spec.checks[0]))
