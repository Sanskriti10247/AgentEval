"""The six tools through DockerWorkspace in a real sandbox (ToolSet.invoke,
so validation, the reaper and output caps are all on the real path)."""

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import docker
import pytest

from agenteval_core.providers.base import ToolCall
from agenteval_core.tools.base import ToolContext, ToolStatus
from agenteval_core.tools.registry import REGISTRY, ToolInvocation, ToolSet
from agenteval_core.tools.workspace import WorkspaceUnavailable
from agenteval_worker.sandbox.session import ContainerSpec, SandboxSession
from agenteval_worker.sandbox.workspace import DockerWorkspace

pytestmark = pytest.mark.docker

IMAGE = "agenteval-sandbox:py312"
LABELS = {"agenteval.test": "tools"}

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = (
    "import unittest\nfrom calc import add\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
FILES = {"calc.py": BUGGY, "test_calc.py": TESTS, "docs/readme.md": "Calc (a+b) helper\n"}


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    c = docker.from_env()
    try:
        c.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{IMAGE} not built; run `make sandbox-image`")
    yield c
    for leftover in c.containers.list(all=True, filters={"label": "agenteval.test=tools"}):
        leftover.remove(force=True)
    c.close()


@pytest.fixture
async def workspace(client: docker.DockerClient) -> AsyncIterator[DockerWorkspace]:
    spec = ContainerSpec(IMAGE, lifetime_s=300, memory_mb=256, cpu_limit=1.0, labels=LABELS)
    session = SandboxSession.open_for_tools(client, spec, FILES)
    yield DockerWorkspace(session)
    session.close()


def context(ws: DockerWorkspace, **kw: Any) -> ToolContext:
    defaults: dict[str, Any] = {
        "workspace": ws,
        "deadline": time.monotonic() + 300,
        "tool_timeout_s": 20,
        "shell_allowlist": frozenset({"python", "sh", "ls"}),
        "test_command": ("python", "-m", "unittest", "-q"),
    }
    return ToolContext(**{**defaults, **kw})


TOOLS = ToolSet.build(sorted(REGISTRY), has_test_command=True)
_ids = iter(range(1, 10_000))


async def run(
    ws: DockerWorkspace, name: str, ctx: ToolContext | None = None, **input: Any
) -> ToolInvocation:
    return await TOOLS.invoke(ctx or context(ws), ToolCall(f"toolu_{next(_ids)}", name, input))


async def test_fix_the_bug_loop(workspace: DockerWorkspace) -> None:
    """The canonical agent task: tests fail, read, fix, tests pass."""
    first = await run(workspace, "run_tests")
    assert first.status is ToolStatus.OK and first.model_content.startswith(
        "Tests failed (exit 1)."
    )
    assert "AssertionError: -1 != 5" in first.model_content

    src = await run(workspace, "read_file", path="calc.py")
    assert src.model_content == BUGGY

    wrote = await run(workspace, "write_file", path="calc.py", content=FIXED)
    assert wrote.status is ToolStatus.OK

    second = await run(workspace, "run_tests")
    assert second.model_content.startswith("Tests passed.") and second.exit_code == 0


async def test_same_size_edit_within_a_second_is_seen_by_python(
    workspace: DockerWorkspace,
) -> None:
    """Regression: a stale .pyc made the fixed code keep failing."""
    for _ in range(3):
        await run(workspace, "write_file", path="calc.py", content=BUGGY)
        assert (await run(workspace, "run_tests")).exit_code == 1
        await run(workspace, "write_file", path="calc.py", content=FIXED)
        assert (await run(workspace, "run_tests")).exit_code == 0
    listing = await run(workspace, "list_dir", depth=1)
    assert "__pycache__" not in listing.model_content


async def test_written_files_get_a_real_mtime(workspace: DockerWorkspace) -> None:
    await run(workspace, "write_file", path="fresh.txt", content="x")
    code = "import os,time; print(int(time.time() - os.path.getmtime('fresh.txt')))"
    age = await run(workspace, "shell", argv=["python", "-c", code])
    assert 0 <= int(age.model_content) < 60


async def test_write_then_read_nested_unicode(workspace: DockerWorkspace) -> None:
    content = "naïve = '✓'\n" * 3
    assert (
        await run(workspace, "write_file", path="a/b/c.py", content=content)
    ).status is ToolStatus.OK
    assert (await run(workspace, "read_file", path="a/b/c.py")).model_content == content


async def test_read_missing_file_is_an_agent_error(workspace: DockerWorkspace) -> None:
    inv = await run(workspace, "read_file", path="nope.py")
    assert inv.status is ToolStatus.ERROR and "No such file" in inv.model_content


async def test_write_over_a_non_empty_directory_is_an_agent_error(
    workspace: DockerWorkspace,
) -> None:
    inv = await run(workspace, "write_file", path="docs", content="x")
    assert inv.status is ToolStatus.ERROR and "Cannot open" in inv.model_content


async def test_list_dir_is_sorted_and_depth_limited(workspace: DockerWorkspace) -> None:
    shallow = await run(workspace, "list_dir", depth=1)
    assert [line.split()[-1] for line in shallow.model_content.splitlines()] == [
        "calc.py", "docs", "test_calc.py",
    ]  # fmt: skip
    deep = await run(workspace, "list_dir", depth=2)
    assert "f 18 docs/readme.md" in deep.model_content.splitlines()
    missing = await run(workspace, "list_dir", path="nowhere")
    assert missing.status is ToolStatus.ERROR and "No such file" in missing.model_content


async def test_search(workspace: DockerWorkspace) -> None:
    hits = await run(workspace, "search", pattern="def add")
    assert hits.model_content == "calc.py:1:def add(a, b):"
    literal = await run(workspace, "search", pattern="(a+b)", fixed_string=True)
    assert literal.model_content == "docs/readme.md:1:Calc (a+b) helper"
    none = await run(workspace, "search", pattern="zzz_not_here")
    assert (none.status, none.model_content) == (ToolStatus.OK, "No matches.")
    bad = await run(workspace, "search", pattern="(unclosed")
    assert bad.status is ToolStatus.ERROR


async def test_search_orders_by_file_then_line_and_caps_results(workspace: DockerWorkspace) -> None:
    lines = "".join(f"hit {i}\n" for i in range(1, 13))
    await run(workspace, "write_file", path="many.txt", content=lines)
    inv = await run(workspace, "search", pattern="hit", path="many.txt", max_results=10)
    out = inv.model_content.splitlines()
    # Numeric line order (not 1, 10, 11, 2 ...), file name always present.
    assert out[:3] == ["many.txt:1:hit 1", "many.txt:2:hit 2", "many.txt:3:hit 3"]
    assert out[9] == "many.txt:10:hit 10"
    assert out[-1] == "[12 matches; showing the first 10]"


async def test_hostile_arguments_are_data_not_shell(workspace: DockerWorkspace) -> None:
    for pattern in ["'; touch pwned; '", '"; touch pwned; "', "$(touch pwned)", "`touch pwned`"]:
        await run(workspace, "search", pattern=pattern, fixed_string=True)
    await run(workspace, "list_dir", path="$(touch pwned)")
    listing = await run(workspace, "list_dir", depth=1)
    assert "pwned" not in listing.model_content


async def test_shell_allowed_denied_and_exit_codes(workspace: DockerWorkspace) -> None:
    ok = await run(
        workspace, "shell", argv=["python", "-c", "import sys; print('hi'); sys.exit(4)"]
    )
    assert ok.status is ToolStatus.OK and ok.model_content == "hi\n[exit code 4]"
    denied = await run(workspace, "shell", argv=["rm", "-rf", "calc.py"])
    assert denied.status is ToolStatus.DENIED
    assert (await run(workspace, "read_file", path="calc.py")).model_content == BUGGY


async def test_shell_timeout_keeps_the_workspace(workspace: DockerWorkspace) -> None:
    t0 = time.monotonic()
    inv = await run(workspace, "shell", context(workspace, tool_timeout_s=1),
                    argv=["sh", "-c", "echo started; sleep 100"])  # fmt: skip
    assert inv.status is ToolStatus.TIMEOUT and inv.model_content.startswith("started\n[timed out")
    assert time.monotonic() - t0 < 5
    assert (await run(workspace, "read_file", path="calc.py")).model_content == BUGGY


# Prints the cmdline of every process except PID 1, the keepalive, itself and
# its parent (the `timeout` wrapper).
PS = (
    "import os,sys\nme=os.getpid();parent=os.getppid();keep=sys.argv[1]\n"
    "for p in sorted(int(x) for x in os.listdir('/proc') if x.isdigit()):\n"
    "    if p in (1, me, parent) or str(p)==keep: continue\n"
    "    print(open(f'/proc/{p}/cmdline','rb').read().replace(b'\\0',b' ').decode())\n"
)


async def test_no_process_survives_a_tool_call(workspace: DockerWorkspace) -> None:
    spawned = await run(
        workspace, "shell", argv=["sh", "-c", "sleep 300 >/dev/null 2>&1 & echo bg"]
    )
    assert spawned.model_content == "bg"
    keep = workspace.session._keepalive_pid
    assert keep is not None
    listing = await run(workspace, "shell", argv=["python", "-c", PS, keep])
    assert listing.model_content == "(no output)"  # the reaper removed the sleep


async def test_state_does_not_carry_between_shell_calls(workspace: DockerWorkspace) -> None:
    await run(workspace, "shell", argv=["sh", "-c", "cd docs && export X=1"])
    where = await run(workspace, "shell", argv=["sh", "-c", 'pwd; echo "X=${X:-unset}"'])
    assert where.model_content == "/workspace\nX=unset"


async def test_sandbox_loss_is_workspace_unavailable(workspace: DockerWorkspace) -> None:
    workspace.session.kill()
    with pytest.raises(WorkspaceUnavailable):
        await run(workspace, "read_file", path="calc.py")
