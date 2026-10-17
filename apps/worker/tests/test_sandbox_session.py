"""SandboxSession against real Docker, locking in the §15 spike results.

For each spike case we check what `timeout` alone leaves behind (before the
reaper) and that nothing survives the reaper. Requires `make sandbox-image`.
"""

import time
from collections.abc import Iterator

import docker
import pytest

from agenteval_worker.sandbox import session as session_mod
from agenteval_worker.sandbox.session import ContainerSpec, SandboxError, SandboxSession

pytestmark = pytest.mark.docker

IMAGE = "agenteval-sandbox:py312"
LABELS = {"agenteval.test": "session"}
SPEC = ContainerSpec(image=IMAGE, lifetime_s=300, memory_mb=256, cpu_limit=1.0, labels=LABELS)

# Lists "pid cmdline" for every process except PID 1, the keepalive, this
# script and its parent (the `timeout` wrapper exec_tool puts around it).
PS = (
    "import os,sys\nme=os.getpid();parent=os.getppid();keep=sys.argv[1]\n"
    "for p in sorted(int(x) for x in os.listdir('/proc') if x.isdigit()):\n"
    "    if p in (1, me, parent) or str(p)==keep: continue\n"
    "    try: c=open(f'/proc/{p}/cmdline','rb').read().replace(b'\\0',b' ').decode().strip()\n"
    "    except OSError: continue\n"
    "    print(p, c.replace('\\n',' '))\n"
)


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    c = docker.from_env()
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{IMAGE} not built; run `make sandbox-image`")
    yield c
    for leftover in c.containers.list(all=True, filters={"label": "agenteval.test=session"}):
        leftover.remove(force=True)
    c.close()


@pytest.fixture
def session(client: docker.DockerClient) -> Iterator[SandboxSession]:
    s = SandboxSession.open_for_tools(client, SPEC, {"hello.txt": "hi\n"})
    yield s
    s.close()


def survivors(s: SandboxSession) -> list[str]:
    assert s._keepalive_pid is not None
    out = s.exec_tool(["python", "-c", PS, s._keepalive_pid], timeout_s=10)
    return [line for line in out.stdout.splitlines() if line]


def assert_clean_after_reap(s: SandboxSession) -> None:
    s.reap()
    assert survivors(s) == []
    assert s.exec_tool(["cat", "hello.txt"], timeout_s=5).stdout == "hi\n"  # still usable


# -- what `timeout` handles on its own ---------------------------------------------------


def test_a_plain_sleep_times_out_and_is_killed(session: SandboxSession) -> None:
    t0 = time.monotonic()
    r = session.exec_tool(["sleep", "100"], timeout_s=2)
    assert r.timed_out and r.exit_code == 124  # sleep honours TERM
    assert 2 <= time.monotonic() - t0 < 4
    assert survivors(session) == []
    assert_clean_after_reap(session)


def test_b_background_and_foreground_children_are_killed_as_a_group(
    session: SandboxSession,
) -> None:
    r = session.exec_tool(["sh", "-c", "sleep 100 & sleep 100"], timeout_s=2)
    assert r.timed_out
    assert survivors(session) == []  # the whole process group got the signal
    assert_clean_after_reap(session)


def test_f_output_before_the_kill_is_kept(session: SandboxSession) -> None:
    r = session.exec_tool(["sh", "-c", "echo before; sleep 100"], timeout_s=2)
    assert r.timed_out and r.stdout == "before\n"


def test_g_fast_command_keeps_its_exit_code(session: SandboxSession) -> None:
    r = session.exec_tool(["sh", "-c", "echo ok; exit 3"], timeout_s=5)
    assert (r.timed_out, r.exit_code, r.stdout) == (False, 3, "ok\n")
    assert r.duration_ms < 2000


def test_h_term_ignoring_process_is_killed_after_kill_after(session: SandboxSession) -> None:
    code = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(100)"
    t0 = time.monotonic()
    r = session.exec_tool(["python", "-c", code], timeout_s=2)
    elapsed = time.monotonic() - t0
    assert r.timed_out and r.exit_code == 137  # KILL one second after TERM
    assert 3 <= elapsed < 5
    assert_clean_after_reap(session)


# -- what only the reaper handles ---------------------------------------------------------


def test_c_background_child_survives_timeout_but_not_the_reaper(session: SandboxSession) -> None:
    r = session.exec_tool(["sh", "-c", "sleep 100 & echo started"], timeout_s=5)
    assert (r.timed_out, r.exit_code, r.stdout) == (False, 0, "started\n")
    assert any("sleep 100" in p for p in survivors(session))  # the gap the spike found
    assert_clean_after_reap(session)


def test_d_detached_background_child_is_reaped(session: SandboxSession) -> None:
    r = session.exec_tool(["sh", "-c", "sleep 100 >/dev/null 2>&1 & echo started"], timeout_s=5)
    assert r.exit_code == 0 and r.duration_ms < 1500  # returns at once
    assert any("sleep 100" in p for p in survivors(session))
    assert_clean_after_reap(session)


SETSID = (
    "import os,time\nif os.fork()==0:\n    os.setsid(){detach}; time.sleep(100)\n"
    "else:\n    {parent}"
)
DETACH = "; fd=os.open('/dev/null',os.O_RDWR); [os.dup2(fd,i) for i in (0,1,2)]"


def test_e_setsid_escapee_holding_stdout_is_reaped(session: SandboxSession) -> None:
    code = SETSID.format(detach="", parent="time.sleep(100)")
    r = session.exec_tool(["python", "-c", code], timeout_s=2)
    assert r.timed_out
    assert len(survivors(session)) == 1  # left its process group: `timeout` can't see it
    assert_clean_after_reap(session)


def test_e2_detached_setsid_escapee_is_reaped(session: SandboxSession) -> None:
    code = SETSID.format(detach=DETACH, parent="print('parent done')")
    r = session.exec_tool(["python", "-c", code], timeout_s=5)
    assert r.exit_code == 0 and not r.timed_out
    assert len(survivors(session)) == 1
    assert_clean_after_reap(session)


def test_i_background_child_holding_the_pipe_delays_the_stream_by_about_2s(
    session: SandboxSession,
) -> None:
    """Docker closes the stream ~2s after the main process exits while an
    orphan still holds stdout (observed in the spike). Bounded, not a hang."""
    t0 = time.monotonic()
    r = session.exec_tool(["sh", "-c", "sleep 30 & echo started"], timeout_s=10)
    elapsed = time.monotonic() - t0
    assert r.exit_code == 0 and not r.timed_out
    assert 1.5 <= elapsed < 4
    assert_clean_after_reap(session)


def test_reaper_keeps_the_pids_limit_from_filling_up(client: docker.DockerClient) -> None:
    """20 calls x 10 detached children = 200 processes > pids_limit 128.
    Only possible because each call is reaped."""
    s = SandboxSession.open_for_tools(client, SPEC, {}, pids_limit=128)
    try:
        spawn = "for i in 1 2 3 4 5 6 7 8 9 10; do sleep 100 >/dev/null 2>&1 & done; echo ok"
        for _ in range(20):
            assert s.exec_tool(["sh", "-c", spawn], timeout_s=5).stdout == "ok\n"
            s.reap()
    finally:
        s.close()


def test_without_the_reaper_leftovers_accumulate(client: docker.DockerClient) -> None:
    """Control for the test above: same calls, no reap, leftovers pile up."""
    s = SandboxSession.open_for_tools(client, SPEC, {}, pids_limit=128)
    try:
        spawn = "for i in 1 2 3 4 5; do sleep 100 >/dev/null 2>&1 & done"
        for _ in range(3):
            s.exec_tool(["sh", "-c", spawn], timeout_s=5)
        assert len(survivors(s)) == 15
    finally:
        s.close()


# -- failure handling -----------------------------------------------------------------------


def test_keepalive_is_recorded_and_preserved(session: SandboxSession) -> None:
    keep = session._keepalive_pid
    assert keep is not None
    session.reap()
    children = session.exec_tool(["cat", "/proc/1/task/1/children"], timeout_s=5).stdout.split()
    assert keep in children
    session.container.reload()
    assert session.container.status == "running"


def test_reaper_failure_is_a_sandbox_error(session: SandboxSession) -> None:
    session.kill()
    with pytest.raises(SandboxError, match="reaper failed"):
        session.reap()


def test_reap_requires_a_recorded_keepalive(client: docker.DockerClient) -> None:
    s = SandboxSession.create(client, SPEC)
    try:
        s.start()
        with pytest.raises(SandboxError, match="keepalive"):
            s.reap()
    finally:
        s.close()


def test_backstop_kills_the_container_if_timeout_never_fires(
    session: SandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a broken in-container `timeout` by giving it a huge value while
    # the host backstop is computed from the real (short) one.
    monkeypatch.setattr(session_mod, "_seconds", lambda _v: "1000")
    monkeypatch.setattr(session_mod, "TOOL_BACKSTOP_MARGIN_S", 0.5)
    t0 = time.monotonic()
    with pytest.raises(SandboxError, match="backstop"):
        session.exec_tool(["sleep", "100"], timeout_s=0.5)
    assert time.monotonic() - t0 < 5
    # `docker kill` only delivers the signal: the exec stream can close (and the
    # backstop return) a few ms before the daemon flips the container's state
    # from "running". The claim is that the container does stop, so wait for that.
    deadline = time.monotonic() + 10
    while True:
        session.container.reload()
        if session.container.status != "running" or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert session.container.status != "running"


def test_open_for_tools_with_missing_image_raises_and_leaks_nothing(
    client: docker.DockerClient,
) -> None:
    missing = ContainerSpec("agenteval-missing-image:none", 60, 128, 1.0, LABELS)
    with pytest.raises(SandboxError, match="image not found"):
        SandboxSession.open_for_tools(client, missing, {})
    assert client.containers.list(all=True, filters={"label": "agenteval.test=session"}) == []


def test_write_files_result_reports_agent_errors_without_raising(session: SandboxSession) -> None:
    # GNU tar silently replaces an *empty* directory; a non-empty one fails.
    session.exec_tool(["mkdir", "-p", "pkg/dir/sub"], timeout_s=5)
    r = session.write_files_result({"pkg/dir": "not a file"})
    assert r.exit_code == 2 and "Cannot open" in r.stderr  # tar explains; the caller decides
    ok = session.write_files_result({"new/nested/file.py": "x = 'é'\n"})
    assert ok.exit_code == 0
    assert session.exec_tool(["cat", "new/nested/file.py"], timeout_s=5).stdout == "x = 'é'\n"


def test_exec_tool_rejects_non_positive_timeouts(session: SandboxSession) -> None:
    with pytest.raises(ValueError):
        session.exec_tool(["true"], timeout_s=0)
