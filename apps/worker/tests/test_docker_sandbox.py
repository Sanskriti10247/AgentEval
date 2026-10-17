"""Real-Docker sandbox tests (S4.3). Requires the image: `make sandbox-image`."""

import time
from collections.abc import Iterator

import docker
import pytest

from agenteval_core.states import Outcome
from agenteval_worker.sandbox.docker_sandbox import (
    DEADLINE_LABEL,
    MANAGED_LABEL,
    DockerSandbox,
    SandboxSpec,
)

pytestmark = pytest.mark.docker

IMAGE = "agenteval-sandbox:py312"


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    c = docker.from_env()
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{IMAGE} not built; run `make sandbox-image`")
    yield c
    c.close()


@pytest.fixture
def sandbox(client: docker.DockerClient) -> DockerSandbox:
    return DockerSandbox(client, max_output_bytes=1024)


def spec(command: list[str], **kw: object) -> SandboxSpec:
    defaults: dict[str, object] = {
        "image": IMAGE,
        "files": {},
        "timeout_s": 20,
        "memory_mb": 128,
        "cpu_limit": 1.0,
        "labels": {"agenteval.test": "1"},
    }
    return SandboxSpec(command=command, **{**defaults, **kw})  # type: ignore[arg-type]


def py(code: str, **kw: object) -> SandboxSpec:
    return spec(["python", "main.py"], files={"main.py": code}, **kw)


def assert_no_leftovers(client: docker.DockerClient) -> None:
    assert client.containers.list(all=True, filters={"label": "agenteval.test=1"}) == []


def test_success(sandbox: DockerSandbox, client: docker.DockerClient) -> None:
    r = sandbox.run(py("print('hi')"))
    assert (r.outcome, r.exit_code, r.stdout, r.stderr) == (Outcome.SUCCESS, 0, "hi\n", "")
    assert r.error_message is None
    assert_no_leftovers(client)


def test_non_zero_exit(sandbox: DockerSandbox, client: docker.DockerClient) -> None:
    r = sandbox.run(spec(["sh", "-c", "echo oops >&2; exit 3"]))
    assert (r.outcome, r.exit_code, r.stderr) == (Outcome.NON_ZERO_EXIT, 3, "oops\n")
    assert_no_leftovers(client)


def test_timeout_kills_and_cleans_up(sandbox: DockerSandbox, client: docker.DockerClient) -> None:
    t0 = time.monotonic()
    r = sandbox.run(spec(["sleep", "999"], timeout_s=2))
    elapsed = time.monotonic() - t0
    assert r.outcome is Outcome.TIMEOUT
    assert 2 <= elapsed < 8, elapsed
    assert_no_leftovers(client)


def test_timeout_keeps_output_produced_before_kill(sandbox: DockerSandbox) -> None:
    r = sandbox.run(py("import time\nprint('started', flush=True)\ntime.sleep(60)", timeout_s=2))
    assert r.outcome is Outcome.TIMEOUT
    assert r.stdout == "started\n"


def test_no_network(sandbox: DockerSandbox) -> None:
    code = (
        "import socket\n"
        "print(len(open('/proc/net/route').read().splitlines()) - 1)\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "except OSError as e:\n"
        "    print('blocked', type(e).__name__)\n"
        "    raise SystemExit(1)\n"
    )
    r = sandbox.run(py(code))
    assert r.outcome is Outcome.NON_ZERO_EXIT
    assert r.stdout.splitlines()[0] == "0"  # no routes: nothing is reachable
    assert r.stdout.splitlines()[1].startswith("blocked")


def test_memory_limit_oom(sandbox: DockerSandbox) -> None:
    r = sandbox.run(py("x = bytearray(400 * 1024 * 1024)\nprint(len(x))", memory_mb=128))
    assert (r.outcome, r.exit_code, r.oom_killed) == (Outcome.NON_ZERO_EXIT, 137, True)


def test_missing_image_is_sandbox_error(
    sandbox: DockerSandbox, client: docker.DockerClient
) -> None:
    r = sandbox.run(spec(["true"], image="agenteval-missing-image:none"))
    assert r.outcome is Outcome.SANDBOX_ERROR
    assert r.exit_code is None
    assert "image not found" in (r.error_message or "")
    assert_no_leftovers(client)


def test_rootfs_is_read_only(sandbox: DockerSandbox) -> None:
    r = sandbox.run(spec(["sh", "-c", "touch /etc/pwned"]))
    assert r.outcome is Outcome.NON_ZERO_EXIT
    assert "Read-only file system" in r.stderr


def test_runs_as_non_root_without_capabilities(sandbox: DockerSandbox) -> None:
    r = sandbox.run(spec(["sh", "-c", "id -u; grep CapEff /proc/self/status"]))
    uid, cap = r.stdout.split("\n")[:2]
    assert uid == "1000"
    assert cap.split()[1] == "0000000000000000"


def test_files_are_written_to_workspace(sandbox: DockerSandbox) -> None:
    files = {
        "main.py": "from pkg.util import v\nprint(v)",
        "pkg/__init__.py": "",
        "pkg/util.py": "v = 42",
    }
    r = sandbox.run(spec(["python", "main.py"], files=files))
    assert (r.outcome, r.stdout) == (Outcome.SUCCESS, "42\n")


def test_pids_limit(sandbox: DockerSandbox) -> None:
    code = (
        "import subprocess\nps=[]\n"
        "try:\n    for _ in range(500): ps.append(subprocess.Popen(['sleep','30']))\n"
        "except OSError as e:\n    print('blocked', len(ps)); raise SystemExit(1)\n"
    )
    r = sandbox.run(py(code))
    assert r.outcome is Outcome.NON_ZERO_EXIT
    assert r.stdout.startswith("blocked")


def test_output_is_truncated(sandbox: DockerSandbox) -> None:
    r = sandbox.run(spec(["sh", "-c", "head -c 100000 /dev/zero | tr '\\0' a; echo e >&2"]))
    assert r.outcome is Outcome.SUCCESS
    assert (len(r.stdout), r.stdout_truncated) == (1024, True)
    assert (r.stderr, r.stderr_truncated) == ("e\n", False)


def test_sweep_removes_only_expired_containers(
    sandbox: DockerSandbox, client: docker.DockerClient
) -> None:
    labels = {MANAGED_LABEL: "true", "agenteval.test": "1"}
    expired = client.containers.create(IMAGE, labels={**labels, DEADLINE_LABEL: "1"})
    live = client.containers.create(
        IMAGE, labels={**labels, DEADLINE_LABEL: f"{time.time() + 600:.0f}"}
    )
    try:
        sandbox.sweep_orphans()
        ids = {
            c.id for c in client.containers.list(all=True, filters={"label": "agenteval.test=1"})
        }
        assert expired.id not in ids
        assert live.id in ids
    finally:
        for c in (expired, live):
            try:
                c.remove(force=True)
            except docker.errors.NotFound:
                pass


# -- security review: hardening the controls above do not already prove ------------------------


def test_no_new_privileges_and_default_seccomp(sandbox: DockerSandbox) -> None:
    r = sandbox.run(spec(["sh", "-c", "grep -E '^(NoNewPrivs|Seccomp):' /proc/self/status"]))
    fields = dict(line.split(":\t") for line in r.stdout.strip().split("\n"))
    assert fields["NoNewPrivs"] == "1"
    assert fields["Seccomp"] == "2"  # filter mode: Docker's default profile is applied


def test_the_workers_environment_never_reaches_the_sandbox(
    sandbox: DockerSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model credential lives in the worker's environment only (README Security model)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SHOULD-NOT-LEAK")
    monkeypatch.setenv("AGENTEVAL_DATABASE_URL", "postgresql://SHOULD-NOT-LEAK")
    r = sandbox.run(spec(["sh", "-c", "env; cat /proc/1/environ | tr '\\0' '\\n'"]))
    assert "SHOULD-NOT-LEAK" not in r.stdout
    assert "ANTHROPIC" not in r.stdout and "AGENTEVAL" not in r.stdout.replace("AGENTEVAL_TEST", "")


def test_the_docker_socket_is_not_visible_in_the_sandbox(sandbox: DockerSandbox) -> None:
    r = sandbox.run(
        spec(["sh", "-c", "test -e /var/run/docker.sock && echo present || echo absent"])
    )
    assert r.stdout.strip() == "absent"


def test_the_sandbox_cannot_reach_the_internal_services(sandbox: DockerSandbox) -> None:
    r = sandbox.run(
        py(
            "import socket\n"
            "for host in ('postgres', 'redis', 'api'):\n"
            "    try:\n"
            "        socket.getaddrinfo(host, 5432)\n"
            "        print(host, 'resolved')\n"
            "    except OSError:\n"
            "        print(host, 'unreachable')\n"
        )
    )
    assert r.stdout.split() == [
        "postgres",
        "unreachable",
        "redis",
        "unreachable",
        "api",
        "unreachable",
    ]


def test_a_sandbox_refuses_an_image_outside_the_workers_allowlist(
    client: docker.DockerClient,
) -> None:
    from agenteval_core.reliability.taxonomy import FailureReason

    strict = DockerSandbox(client, image_allowlist=["some-other-image:1"])
    r = strict.run(spec(["true"]))  # IMAGE exists locally but is not allowed
    assert r.outcome is Outcome.SANDBOX_ERROR
    assert r.failure_reason is FailureReason.SANDBOX_IMAGE_NOT_FOUND
    assert "allowlist" in (r.error_message or "")
    assert_no_leftovers(client)


def test_writable_mounts_are_noexec_nosuid_nodev(sandbox: DockerSandbox) -> None:
    """Docker's tmpfs defaults, relied on rather than set (verified here so an engine change
    would be noticed)."""
    r = sandbox.run(spec(["sh", "-c", "grep -E ' /(workspace|tmp) ' /proc/mounts"]))
    lines = r.stdout.strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        flags = set(line.split()[3].split(","))
        assert {"nosuid", "nodev", "noexec"} <= flags, line
