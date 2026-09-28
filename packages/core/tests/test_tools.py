"""Tools and ToolSet against a scripted workspace (no Docker)."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from agenteval_core.providers.base import ToolCall
from agenteval_core.tools.base import (
    MIN_TOOL_TIME_S,
    ToolContext,
    ToolInput,
    ToolStatus,
    _strip_titles,
    format_exec,
)
from agenteval_core.tools.registry import (
    MODEL_OUTPUT_LIMIT,
    REGISTRY,
    STORED_OUTPUT_LIMIT,
    ToolInvocation,
    ToolSet,
    cap_for_model,
    unknown_tool_names,
)
from agenteval_core.tools.workspace import ExecOutput, WorkspaceUnavailable

ALL = sorted(REGISTRY)


@dataclass
class FakeWorkspace:
    exec_results: list[ExecOutput | Exception] = field(default_factory=list)
    write_result: ExecOutput = field(default_factory=lambda: ExecOutput(0, "", ""))
    reap_error: Exception | None = None
    execs: list[tuple[list[str], float]] = field(default_factory=list)
    writes: list[dict[str, str]] = field(default_factory=list)
    reaps: int = 0

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecOutput:
        self.execs.append((argv, timeout_s))
        result = self.exec_results.pop(0) if self.exec_results else ExecOutput(0, "", "")
        if isinstance(result, Exception):
            raise result
        return result

    async def write_files(self, files: dict[str, str]) -> ExecOutput:
        self.writes.append(files)
        return self.write_result

    async def reap(self) -> None:
        self.reaps += 1
        if self.reap_error:
            raise self.reap_error

    async def abort(self) -> None:
        pass


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def ctx(
    ws: FakeWorkspace,
    *,
    time_left: float = 600,
    tool_timeout_s: float = 60,
    allow: tuple[str, ...] = ("python", "ls"),
    test_command: tuple[str, ...] | None = ("python", "-m", "unittest"),
) -> ToolContext:
    clock = Clock()
    return ToolContext(
        workspace=ws,
        deadline=clock.now + time_left,
        tool_timeout_s=tool_timeout_s,
        shell_allowlist=frozenset(allow),
        test_command=test_command,
        clock=clock,
    )


def call(name: str, **input: Any) -> ToolCall:
    return ToolCall(id="toolu_1", name=name, input=input)


async def invoke(
    ws: FakeWorkspace, name: str, c: ToolContext | None = None, **input: Any
) -> ToolInvocation:
    toolset = ToolSet.build(ALL, has_test_command=True)
    return await toolset.invoke(c or ctx(ws), call(name, **input))


# -- registry / specs -------------------------------------------------------------------


def test_registry_has_the_six_builtin_tools() -> None:
    assert ALL == ["list_dir", "read_file", "run_tests", "search", "shell", "write_file"]


def test_unknown_names_are_reported() -> None:
    assert unknown_tool_names(["read_file", "browse", "exec"]) == ["browse", "exec"]
    with pytest.raises(ValueError, match="browse"):
        ToolSet.build(["read_file", "browse"], has_test_command=False)


def test_run_tests_is_only_offered_when_the_task_has_tests() -> None:
    assert "run_tests" not in ToolSet.build(ALL, has_test_command=False).names
    assert "run_tests" in ToolSet.build(ALL, has_test_command=True).names


def test_specs_are_sorted_and_deduplicated() -> None:
    ts = ToolSet.build(["write_file", "read_file", "read_file"], has_test_command=False)
    assert [s.name for s in ts.specs()] == ["read_file", "write_file"]


def _walk(node: Any) -> list[Any]:
    out = [node]
    if isinstance(node, dict):
        for v in node.values():
            out += _walk(v)
    elif isinstance(node, list):
        for v in node:
            out += _walk(v)
    return out


@pytest.mark.parametrize("name", ALL)
def test_schemas_are_strict_objects_without_titles(name: str) -> None:
    schema = REGISTRY[name].input_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert not any(isinstance(n, dict) and "title" in n for n in _walk(schema))


def test_required_fields() -> None:
    assert REGISTRY["read_file"].input_schema()["required"] == ["path"]
    assert set(REGISTRY["write_file"].input_schema()["required"]) == {"path", "content"}
    assert "required" not in REGISTRY["run_tests"].input_schema()


def test_strip_titles_keeps_a_field_named_title() -> None:
    class WithTitle(ToolInput):
        title: str

    schema = _strip_titles(WithTitle.model_json_schema())
    assert "title" in schema["properties"]
    assert "title" not in schema and "title" not in schema["properties"]["title"]


# -- ToolSet.invoke: agent-level problems become is_error results -------------------------


async def test_unknown_tool_is_an_error_result_without_exec_or_reap() -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, "browse", url="x")
    assert inv.status is ToolStatus.UNKNOWN_TOOL and inv.is_error
    assert "Available tools: list_dir, read_file" in inv.model_content
    assert (ws.execs, ws.reaps) == ([], 0)


@pytest.mark.parametrize(
    ("name", "input", "fragment"),
    [
        ("read_file", {}, "path: Field required"),
        ("read_file", {"path": "../etc/passwd"}, "path"),
        ("read_file", {"path": "/etc/passwd"}, "relative"),
        ("read_file", {"path": "a.py", "mode": "rb"}, "mode: Extra inputs are not permitted"),
        ("list_dir", {"depth": 9}, "depth"),
        ("shell", {"argv": []}, "argv"),
        ("shell", {"argv": "rm -rf /"}, "argv"),
        ("search", {"pattern": ""}, "pattern"),
    ],
)
async def test_invalid_input_is_an_error_result_without_exec_or_reap(
    name: str, input: dict[str, Any], fragment: str
) -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, name, **input)
    assert inv.status is ToolStatus.INVALID_INPUT
    assert fragment in inv.model_content
    assert (ws.execs, ws.reaps) == ([], 0)


async def test_reaper_runs_after_every_executed_call() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "x", ""), ExecOutput(1, "", "boom")])
    await invoke(ws, "read_file", path="a")
    await invoke(ws, "read_file", path="b")  # failing call
    await invoke(ws, "shell", argv=["rm", "-rf", "/"])  # denied call
    assert ws.reaps == 3


async def test_reaper_runs_even_when_the_workspace_fails_mid_call() -> None:
    ws = FakeWorkspace(exec_results=[WorkspaceUnavailable("daemon gone")])
    with pytest.raises(WorkspaceUnavailable):
        await invoke(ws, "read_file", path="a")
    assert ws.reaps == 1


async def test_reaper_failure_propagates_as_workspace_unavailable() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "ok", "")],
                       reap_error=WorkspaceUnavailable("reaper failed"))  # fmt: skip
    with pytest.raises(WorkspaceUnavailable, match="reaper failed"):
        await invoke(ws, "read_file", path="a")


async def test_unexpected_exceptions_are_not_swallowed() -> None:
    ws = FakeWorkspace(exec_results=[RuntimeError("bug")])
    with pytest.raises(RuntimeError, match="bug"):
        await invoke(ws, "read_file", path="a")
    assert ws.reaps == 1


# -- time budget ---------------------------------------------------------------------------


async def test_timeout_is_the_smaller_of_tool_timeout_and_time_left() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "", ""), ExecOutput(0, "", "")])
    await invoke(ws, "read_file", ctx(ws, time_left=600, tool_timeout_s=60), path="a")
    await invoke(ws, "read_file", ctx(ws, time_left=7.5, tool_timeout_s=60), path="a")
    assert [t for _, t in ws.execs] == [60, 7.5]


async def test_no_time_left_is_a_timeout_without_exec() -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, "read_file", ctx(ws, time_left=MIN_TOOL_TIME_S / 2), path="a")
    assert inv.status is ToolStatus.TIMEOUT and "no time left" in inv.model_content
    assert ws.execs == []


# -- individual tools ------------------------------------------------------------------------


async def test_read_file() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "print(1)\n", "")])
    inv = await invoke(ws, "read_file", path="src/app.py")
    assert ws.execs[0][0] == ["cat", "--", "src/app.py"]
    assert (inv.status, inv.model_content, inv.exit_code) == (ToolStatus.OK, "print(1)\n", 0)


async def test_read_file_failure_and_timeout() -> None:
    ws = FakeWorkspace(exec_results=[
        ExecOutput(1, "", "cat: nope.py: No such file or directory\n"),
        ExecOutput(124, "", "", timed_out=True),
    ])  # fmt: skip
    missing = await invoke(ws, "read_file", path="nope.py")
    assert missing.status is ToolStatus.ERROR and "No such file" in missing.model_content
    slow = await invoke(ws, "read_file", path="fifo")
    assert slow.status is ToolStatus.TIMEOUT and "timed out" in slow.model_content


async def test_write_file() -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, "write_file", path="pkg/new.py", content="x = 'é'\n")
    assert ws.writes == [{"pkg/new.py": "x = 'é'\n"}]
    assert inv.status is ToolStatus.OK and "Wrote 9 bytes to pkg/new.py" in inv.model_content
    assert ws.execs == []


async def test_write_file_failure_is_an_agent_error() -> None:
    ws = FakeWorkspace(write_result=ExecOutput(2, "", "tar: pkg: Cannot open: File exists\n"))
    inv = await invoke(ws, "write_file", path="pkg", content="x")
    assert inv.status is ToolStatus.ERROR and "Cannot open" in inv.model_content


async def test_list_dir_passes_arguments_positionally() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "d 60 pkg\nf 9 pkg/a.py\n", ""),
                                     ExecOutput(0, "", "")])  # fmt: skip
    inv = await invoke(ws, "list_dir", path="pkg", depth=3)
    argv = ws.execs[0][0]
    assert argv[:2] == ["sh", "-c"] and argv[3:] == ["list_dir", "pkg", "3"]
    assert inv.model_content == "d 60 pkg\nf 9 pkg/a.py"
    empty = await invoke(ws, "list_dir")
    assert empty.model_content == "(empty directory)" and ws.execs[1][0][-2:] == [".", "2"]


@pytest.mark.parametrize(("fixed", "flag"), [(False, "-E"), (True, "-F")])
async def test_search_modes(fixed: bool, flag: str) -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(0, "a.py:3:def f():\n", "")])
    inv = await invoke(ws, "search", pattern="def f(", fixed_string=fixed, max_results=5)
    assert ws.execs[0][0][3:] == ["search", "def f(", ".", "5", flag]
    assert inv.model_content == "a.py:3:def f():"


async def test_search_no_matches_is_ok_and_errors_are_errors() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(1, "", ""),
                                     ExecOutput(2, "", "grep: Unmatched ( or \\(\n")])  # fmt: skip
    none = await invoke(ws, "search", pattern="zzz")
    assert (none.status, none.model_content) == (ToolStatus.OK, "No matches.")
    bad = await invoke(ws, "search", pattern="(")
    assert bad.status is ToolStatus.ERROR and "Unmatched" in bad.model_content


@pytest.mark.parametrize(
    ("argv", "allow"),
    [
        (["rm", "-rf", "/"], ("python",)),
        (["/usr/bin/python", "x.py"], ("python",)),  # paths are never allowed
        (["python"], ()),  # empty allowlist
    ],
)
async def test_shell_denies_commands_off_the_allowlist(
    argv: list[str], allow: tuple[str, ...]
) -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, "shell", ctx(ws, allow=allow), argv=argv)
    assert inv.status is ToolStatus.DENIED and "not an allowed command" in inv.model_content
    assert ws.execs == []


async def test_shell_passes_argv_verbatim_and_reports_exit_codes() -> None:
    hostile = "; touch /tmp/pwned #"
    ws = FakeWorkspace(exec_results=[ExecOutput(1, "", "Traceback ...\n")])
    inv = await invoke(ws, "shell", argv=["python", "-c", hostile])
    assert ws.execs[0][0] == ["python", "-c", hostile]  # one argv element, no shell
    assert inv.status is ToolStatus.OK  # a failing command is information, not a tool error
    assert inv.model_content == "[stderr]\nTraceback ...\n[exit code 1]"


async def test_shell_timeout() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(124, "partial\n", "", timed_out=True)])
    inv = await invoke(ws, "shell", argv=["python", "loop.py"])
    assert inv.status is ToolStatus.TIMEOUT and inv.model_content.startswith("partial\n[timed out")


async def test_run_tests_verdicts() -> None:
    ws = FakeWorkspace(exec_results=[ExecOutput(1, "", "FAILED (failures=1)\n"),
                                     ExecOutput(0, "", "OK\n")])  # fmt: skip
    failed = await invoke(ws, "run_tests")
    assert ws.execs[0][0] == ["python", "-m", "unittest"]
    assert failed.status is ToolStatus.OK and failed.model_content.startswith(
        "Tests failed (exit 1)."
    )
    passed = await invoke(ws, "run_tests")
    assert passed.model_content.startswith("Tests passed.")


async def test_run_tests_without_a_test_command_is_an_error() -> None:
    ws = FakeWorkspace()
    inv = await invoke(ws, "run_tests", ctx(ws, test_command=None))
    assert inv.status is ToolStatus.ERROR and ws.execs == []


# -- output shaping -----------------------------------------------------------------------------


def test_format_exec() -> None:
    assert format_exec(ExecOutput(0, "", "")) == "(no output)"
    assert format_exec(ExecOutput(0, "a\n", "b\n")) == "a\n[stderr]\nb"
    assert format_exec(ExecOutput(3, "", "")) == "[exit code 3]"
    assert format_exec(ExecOutput(0, "x", "", stdout_truncated=True)).endswith(
        "[output truncated at the sandbox limit]"
    )
    assert "timed out after 5s" in format_exec(ExecOutput(124, "", "", timed_out=True), timeout_s=5)


def test_model_cap_marks_truncation_and_never_splits_a_character() -> None:
    assert cap_for_model("short") == "short"
    text = "é" * MODEL_OUTPUT_LIMIT  # 2 bytes each: twice the limit
    capped = cap_for_model(text)
    head, marker = capped.rsplit("\n", 1)
    assert set(head) == {"é"} and len(head.encode()) <= MODEL_OUTPUT_LIMIT
    total = 2 * MODEL_OUTPUT_LIMIT
    assert marker == f"[output truncated: showing the first {MODEL_OUTPUT_LIMIT} of {total} bytes]"


async def test_model_and_stored_caps() -> None:
    big = "x" * (STORED_OUTPUT_LIMIT * 2)
    ws = FakeWorkspace(exec_results=[ExecOutput(0, big, "")])
    inv = await invoke(ws, "read_file", path="big.txt")
    assert len(inv.model_content.encode()) < MODEL_OUTPUT_LIMIT + 100
    assert len(inv.stored_content.encode()) < STORED_OUTPUT_LIMIT + 100
    assert inv.stored_content.endswith(
        f"[output truncated: showing the first {STORED_OUTPUT_LIMIT} of {len(big)} bytes]"
    )
