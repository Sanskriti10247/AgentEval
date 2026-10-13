"""Targeted security tests for the evaluation sandbox (Phase 4 step 4).

Areas: archive traversal, hidden-file isolation, no network, read-only /eval,
timeout/reaping, snapshot completeness - plus the spike's grading attacks run
through the real Step 3 hardened plans. Attacks run as CANDIDATE CODE inside
plans wherever possible, because that is the threat during grading.
"""

import io
import tarfile
from collections.abc import Callable
from typing import Any

import pytest

from agenteval_core.evaluation.checks import judge, plan
from agenteval_core.evaluation.results import ExecPlan
from agenteval_core.evaluation.spec import EvaluationSpec
from agenteval_worker.sandbox.evaluation import EvaluationSession, SnapshotError, validate_snapshot
from agenteval_worker.sandbox.session import SANDBOX_USER
from agenteval_worker.sandbox.workspace import _to_output

pytestmark = pytest.mark.docker

MARKER = "HIDDEN_MARKER_e4c1"
BUGGY = "def add(a, b):\n    return a - b\n\ndef mul(a, b):\n    return 0\n"
HIDDEN = {
    "test_a.py": f"# {MARKER}\nimport unittest\nfrom calc import add\n\n"
    "class A(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(2, 3), 5)\n",
    "test_b.py": "import unittest\nfrom calc import mul\n\n"
    "class B(unittest.TestCase):\n    def test_b(self):\n        self.assertEqual(mul(2, 3), 6)\n",
}
SPEC = EvaluationSpec.model_validate(
    {
        "checks": [{"kind": "python_tests", "name": "hidden", "expected_tests": 2}],
        "hidden_files": HIDDEN,
    }
)


def run(session: EvaluationSession, script: str, **kw: Any) -> Any:
    return session.run_plan(ExecPlan("probe", ["sh", "-c", script], 20, **kw))


def grade(session: EvaluationSession) -> Any:
    check = SPEC.checks[0]
    return judge(check, _to_output(session.run_plan(plan(check))))


# -- 1. archive traversal ----------------------------------------------------------------------


def crafted(*members: tuple[str, str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for kind, name, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(payload)
                t.addfile(ti, io.BytesIO(payload.encode()))
            else:
                ti.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE
                ti.linkname = payload
                t.addfile(ti)
    return buf.getvalue()


@pytest.mark.parametrize(
    "members",
    [
        [("file", "/tmp/abs.txt", "x")],
        [("file", "../../tmp/trav.txt", "x")],
        [("lnk", "h", "/eval/.agenteval/run_unittest.py")],
    ],
    ids=["absolute", "traversal", "hardlink-out"],
)
def test_validation_rejects_traversal_before_any_extraction(
    members: list[tuple[str, str, str]],
) -> None:
    with pytest.raises(SnapshotError, match="invalid"):
        validate_snapshot(crafted(*members), max_unpacked_bytes=10_000)


@pytest.mark.parametrize(
    "members",
    [
        [("file", "/tmp/abs.txt", "x")],
        [("file", "../../tmp/trav.txt", "x")],
        [("sym", "d", "/tmp"), ("file", "d/evil.txt", "x")],
        [("sym", "e", "/eval"), ("file", "e/evil.py", "x")],
        [("lnk", "h", "/eval/.agenteval/run_unittest.py")],
    ],
    ids=[
        "absolute",
        "traversal",
        "symlink-write-through-tmp",
        "symlink-write-through-eval",
        "hardlink-out",
    ],
)
def test_even_unvalidated_archives_cannot_write_outside_workspace(
    agent_snapshot: Callable[..., Any],
    eval_session: Callable[..., EvaluationSession],
    members: list[tuple[str, str, str]],
) -> None:
    """Defence in depth: bypass host validation and feed tar directly, as the
    candidate's uid. GNU tar must still write nothing outside /workspace."""
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap, SPEC)
    session._tar_in(crafted(*members), "/workspace", SANDBOX_USER, compressed=True)
    escaped = run(
        session,
        "ls /tmp/abs.txt /tmp/trav.txt /tmp/evil.txt /eval/evil.py 2>/dev/null",
        fresh_workspace=False,
    )
    assert escaped.stdout.strip() == ""
    assert (
        run(
            session, "stat -c %U:%a /eval/.agenteval/run_unittest.py", fresh_workspace=False
        ).stdout.strip()
        == "root:444"
    )


# -- 2. hidden-file isolation ----------------------------------------------------------------


def test_hidden_files_never_reach_the_agent_container_or_snapshot(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    agent, snap = agent_snapshot({"calc.py": BUGGY})
    eval_session(snap, SPEC)  # hidden files now exist - in another container
    probe = agent.exec_tool(
        [
            "sh",
            "-c",
            f"grep -rls {MARKER} / --exclude-dir=proc --exclude-dir=sys 2>/dev/null; "
            "echo done; ls -d /eval 2>&1",
        ],
        timeout_s=30,
    )
    assert probe.stdout.splitlines()[0] == "done"
    assert "No such file" in probe.stdout
    assert MARKER.encode() not in _unpacked(snap.data)


def test_hidden_files_live_only_in_eval_never_in_workspace(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap, SPEC)
    found = run(session, f"grep -rls {MARKER} /workspace /tmp; grep -rls {MARKER} /eval")
    assert found.stdout.split() == ["/eval/test_a.py"]


def _unpacked(data: bytes) -> bytes:
    out = b""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t:
            if m.isreg():
                f = t.extractfile(m)
                out += f.read() if f else b""
    return out


# -- 3. no network ---------------------------------------------------------------------------------


def test_evaluation_container_has_no_network(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap)
    probe = run(
        session,
        'echo routes=$(($(wc -l < /proc/net/route) - 1)); python -c "import socket; '
        "socket.create_connection(('1.1.1.1', 80), timeout=3)\" 2>&1 | tail -1",
    )
    assert "routes=0" in probe.stdout and "unreachable" in probe.stdout.lower()


def test_container_hardening(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap)
    mounts = run(session, "grep -E ' /(workspace|eval|tmp) ' /proc/mounts").stdout.splitlines()
    assert len(mounts) == 3
    for line in mounts:
        opts = line.split()[3].split(",")
        assert {"noexec", "nosuid", "nodev"} <= set(opts), line
    status = run(
        session, "grep -E 'CapEff|NoNewPrivs' /proc/self/status; id -u; touch /etc/x 2>&1 | tail -1"
    ).stdout
    assert "0000000000000000" in status and "NoNewPrivs:\t1" in status
    assert "\n1000\n" in status and "Read-only file system" in status


# -- 4. read-only /eval ----------------------------------------------------------------------------


ATTACKS = {
    "append": "echo x >> /eval/test_a.py",
    "truncate": ": > /eval/test_a.py",
    "delete": "rm -f /eval/test_a.py",
    "rename": "mv /eval/test_a.py /eval/x.py",
    "create": "touch /eval/new.py",
    "create .nopyc": "mkdir /eval/.nopyc",
    "chmod": "chmod 666 /eval/test_a.py",
    "replace runner": "cp /dev/null /eval/.agenteval/run_unittest.py",
    "symlink over": "ln -sf /workspace/calc.py /eval/test_b.py",
}


def test_candidate_code_cannot_modify_eval(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap, SPEC)
    for label, attack in ATTACKS.items():
        assert run(session, attack).exit_code != 0, label
    state = run(
        session,
        "stat -c '%U %a %n' /eval /eval/test_a.py /eval/.agenteval/run_unittest.py; ls -A /eval",
    ).stdout
    assert "root 555 /eval" in state and "root 444 /eval/test_a.py" in state
    assert ".nopyc" not in state and "x.py" not in state and "new.py" not in state


# -- 5. timeout and reaping ------------------------------------------------------------------------


def test_hanging_plan_is_killed_and_every_child_reaped(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY})
    session = eval_session(snap)
    escape = (
        "import os,time\nif os.fork()==0:\n    os.setsid(); time.sleep(300)\n"
        "else:\n    time.sleep(300)\n"
    )
    hang = session.run_plan(ExecPlan("hang", ["sh", "-c", f"sleep 300 & python -c '{escape}'"], 2))
    assert hang.timed_out
    table = run(
        session,
        "for p in /proc/[0-9]*; do printf '%s ' ${p#/proc/}; "
        "tr '\\0' ' ' < $p/cmdline 2>/dev/null; echo; done",
        fresh_workspace=False,
    )
    survivors = [
        line
        for line in table.stdout.splitlines()
        if line.split(" ", 1)[0] not in ("1", session._keepalive_pid)
        and ("sleep 300" in line or "python -c" in line)
        and "/proc/[0-9]*" not in line  # the probe's own shell and timeout wrapper
    ]
    assert survivors == []
    assert run(session, "echo alive").stdout.strip() == "alive"


# -- 6. snapshot completeness (C1) -----------------------------------------------------------------


def test_unreadable_files_and_dirs_are_exported_not_dropped(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    work = (
        "mkdir -p locked/inner && printf 'deep' > locked/inner/f.txt && printf 'top' > hidden.txt "
        "&& chmod 000 hidden.txt locked/inner/f.txt && chmod 000 locked/inner && chmod 000 locked"
    )
    _, snap = agent_snapshot({"calc.py": BUGGY}, work)
    assert set(snap.normalized_paths) >= {
        "hidden.txt",
        "locked",
        "locked/inner",
        "locked/inner/f.txt",
    }
    session = eval_session(snap)
    got = run(session, "cat hidden.txt; echo; cat locked/inner/f.txt").stdout
    assert got.split() == ["top", "deep"]


def test_member_tar_would_skip_fails_the_export_explicitly(
    agent_snapshot: Callable[..., Any],
) -> None:
    """GNU tar skips unix sockets with a warning and exit 0; C1 forbids that
    silent loss, so the export must raise instead."""
    bind = "python -c \"import socket; socket.socket(socket.AF_UNIX).bind('sock')\""
    with pytest.raises(SnapshotError, match="socket ignored") as exc:
        agent_snapshot({"calc.py": BUGGY}, bind)
    assert exc.value.reason == "incomplete"


# -- grading attacks through the real hardened plans (spike findings) ------------------------------


@pytest.mark.parametrize(
    ("label", "files", "work"),
    [
        ("sitecustomize", {"sitecustomize.py": "import calc\ncalc.add = lambda a, b: a + b\n"}, ""),
        (
            "stdlib shadowing",
            {"unittest.py": "import sys; sys.exit(0)\n", "json.py": "raise SystemExit(0)\n"},
            "",
        ),
        (
            "forged workspace pyc",
            {},
            (
                "cp calc.py calc.src && printf 'def add(a, b):\\n    return a + b\\n\\n"
                "def mul(a, b):\\n"
                "    return a * b\\n' > calc.py && truncate -s $(stat -c %s calc.src) calc.py && "
                'touch -r calc.src calc.py && python -c "import py_compile; '
                "py_compile.compile('calc.py', cfile='__pycache__/calc.cpython-312.pyc')\" && "
                "cp calc.src calc.py && touch -r calc.src calc.py && rm calc.src"
            ),
        ),
    ],
)
def test_workspace_planted_attacks_do_not_change_the_grade(
    agent_snapshot: Callable[..., Any],
    eval_session: Callable[..., EvaluationSession],
    label: str,
    files: dict[str, str],
    work: str,
) -> None:
    _, snap = agent_snapshot({"calc.py": BUGGY, **files}, work)
    result = grade(eval_session(snap, SPEC))
    assert result.passed is False, label
    assert result.tests_total == 2 and result.tests_passed == 0


def test_candidate_cannot_forge_a_later_hidden_test_bytecode(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    """The spike's cross-module attack: code imported by test_a forges the
    cached bytecode of test_b. With pycache_prefix=/eval/.nopyc it can't."""
    forge = (
        "import os, sys, py_compile, importlib.util\n"
        "def add(a, b): return a + b\n"
        "def mul(a, b): return 0\n"
        "src = '/eval/test_b.py'\n"
        "if sys.pycache_prefix:\n"
        "    st = os.stat(src); orig = 'self.assertEqual(mul(2, 3), 6)'\n"
        "    forged = open(src).read().replace(orig, 'self.assertTrue(True)'.ljust(len(orig)))\n"
        "    tmp = '/tmp/forge_b.py'; open(tmp, 'w').write(forged)\n"
        "    os.utime(tmp, (st.st_atime, st.st_mtime))\n"
        "    try:\n"
        "        target = importlib.util.cache_from_source(src)\n"
        "        os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "        py_compile.compile(tmp, cfile=target, dfile=src)\n"
        "    except OSError:\n"
        "        pass\n"
    )
    _, snap = agent_snapshot({"calc.py": forge})
    result = grade(eval_session(snap, SPEC))
    assert result.passed is False and result.tests_passed == 1  # mul() still caught


def test_run_results_are_transient_only(
    agent_snapshot: Callable[..., Any], eval_session: Callable[..., EvaluationSession]
) -> None:
    """D11: raw output (here, a candidate dumping /eval) exists only in the
    transient ExecResult; the judged CheckResult never carries it."""
    leak = "import sys\nprint(open('/eval/test_a.py').read(), file=sys.stderr)\nsys.exit(1)\n"
    _, snap = agent_snapshot({"calc.py": leak})
    session = eval_session(snap, SPEC)
    raw = session.run_plan(plan(SPEC.checks[0]))
    assert MARKER in raw.stderr  # the candidate can READ hidden tests while graded (known)
    assert MARKER not in judge(SPEC.checks[0], _to_output(raw)).model_dump_json()
