"""The six built-in tools. Each runs as an exec inside the attempt's sandbox.

Model-supplied values are passed as separate argv elements or as positional
parameters ($1, $2, ...) to a fixed `sh -c` script, never interpolated into
a shell string. Paths are relative to /workspace and validated.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field

from agenteval_core.schemas.task import MAX_FILES_BYTES, validate_relative_path
from agenteval_core.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolDenied,
    ToolFailed,
    ToolInput,
    ToolOutput,
    ToolStatus,
    format_exec,
    run_exec,
)


def _file_path(value: str) -> str:
    validate_relative_path(value)
    return value


def _dir_path(value: str) -> str:
    if value != ".":
        validate_relative_path(value)
    return value


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL bytes")
    return value


FilePath = Annotated[
    str,
    AfterValidator(_file_path),
    Field(description="Path relative to /workspace, e.g. 'src/app.py'."),
]
DirPath = Annotated[
    str,
    AfterValidator(_dir_path),
    Field(description="Directory relative to /workspace; '.' is the workspace root."),
]


def _timed_out(out_timeout: bool, content: str) -> ToolOutput | None:
    return ToolOutput(content, ToolStatus.TIMEOUT) if out_timeout else None


# -- read_file ----------------------------------------------------------------------


class ReadFileInput(ToolInput):
    path: FilePath


class ReadFile(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace."
    input_model = ReadFileInput
    permission = Permission.READ_FS

    async def execute(self, ctx: ToolContext, args: ReadFileInput) -> ToolOutput:
        out = await run_exec(ctx, ["cat", "--", args.path])
        if isinstance(out, ToolOutput):
            return out
        if timed := _timed_out(out.timed_out, format_exec(out, timeout_s=ctx.tool_timeout_s)):
            return timed
        if out.exit_code != 0:
            raise ToolFailed(out.stderr.strip() or f"could not read {args.path}")
        note = "\n[file truncated at the sandbox output limit]" if out.stdout_truncated else ""
        return ToolOutput(out.stdout + note, exit_code=0)


# -- write_file ---------------------------------------------------------------------


class WriteFileInput(ToolInput):
    path: FilePath
    content: Annotated[str, Field(max_length=MAX_FILES_BYTES, description="Full file content.")]


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a file in the workspace with the given content. "
        "Parent directories are created as needed."
    )
    input_model = WriteFileInput
    permission = Permission.WRITE_FS

    async def execute(self, ctx: ToolContext, args: WriteFileInput) -> ToolOutput:
        size = len(args.content.encode())
        if size > MAX_FILES_BYTES:
            raise ToolFailed(f"content is {size} bytes; the limit is {MAX_FILES_BYTES}")
        out = await ctx.workspace.write_files({args.path: args.content})
        if out.exit_code != 0:
            raise ToolFailed(f"could not write {args.path}: {out.stderr.strip() or out.exit_code}")
        return ToolOutput(f"Wrote {size} bytes to {args.path}.", exit_code=0)


# -- list_dir -----------------------------------------------------------------------

# Sorted, one entry per line: "<type> <size> <path>" (type: f file, d dir, l link).
_LIST_SCRIPT = (
    'out=$(LC_ALL=C find "$1" -mindepth 1 -maxdepth "$2" -printf "%y %s %P\\n"); rc=$?; '
    '[ -n "$out" ] && printf "%s\\n" "$out" | LC_ALL=C sort -k3; exit $rc'
)


class ListDirInput(ToolInput):
    path: DirPath = "."
    depth: Annotated[int, Field(ge=1, le=3, description="How many levels to descend.")] = 2


class ListDir(Tool):
    name = "list_dir"
    description = (
        "List files and directories under a workspace path, sorted by path. "
        "Each line is '<type> <size_bytes> <path>' where type is f (file), d (dir) or l (link)."
    )
    input_model = ListDirInput
    permission = Permission.READ_FS

    async def execute(self, ctx: ToolContext, args: ListDirInput) -> ToolOutput:
        out = await run_exec(
            ctx, ["sh", "-c", _LIST_SCRIPT, "list_dir", args.path, str(args.depth)]
        )
        if isinstance(out, ToolOutput):
            return out
        if timed := _timed_out(out.timed_out, format_exec(out, timeout_s=ctx.tool_timeout_s)):
            return timed
        if out.exit_code != 0:
            raise ToolFailed(out.stderr.strip() or f"could not list {args.path}")
        return ToolOutput(out.stdout.rstrip("\n") or "(empty directory)", exit_code=0)


# -- search -------------------------------------------------------------------------

# grep exit: 0 matches, 1 no matches, 2 error. Output sorted by file then line.
# grep exit: 0 matches, 1 no matches, 2 error. -H always prints the file name
# (a single-file search would otherwise omit it and break the sort). Searching
# "." without an operand yields clean relative paths (no "./" prefix).
# Output is sorted by file, then numerically by line.
_SEARCH_SCRIPT = (
    'if [ "$2" = "." ]; then out=$(grep -rnIH "$4" -e "$1"); '
    'else out=$(grep -rnIH "$4" -e "$1" -- "$2"); fi; rc=$?; '
    'if [ -n "$out" ]; then n=$(printf "%s\\n" "$out" | wc -l); '
    'printf "%s\\n" "$out" | LC_ALL=C sort -t: -k1,1 -k2,2n | head -n "$3"; '
    '[ "$n" -gt "$3" ] && echo "[$n matches; showing the first $3]"; fi; exit $rc'
)


class SearchInput(ToolInput):
    pattern: Annotated[
        str,
        AfterValidator(_no_nul),
        Field(min_length=1, max_length=1000, description="Regular expression (grep -E)."),
    ]
    path: DirPath = "."
    fixed_string: Annotated[
        bool, Field(description="Treat the pattern as a literal string instead of a regex.")
    ] = False
    max_results: Annotated[int, Field(ge=1, le=500)] = 100


class Search(Tool):
    name = "search"
    description = (
        "Search text files under a workspace path. Returns 'path:line:text' lines "
        "sorted by path and line number."
    )
    input_model = SearchInput
    permission = Permission.READ_FS

    async def execute(self, ctx: ToolContext, args: SearchInput) -> ToolOutput:
        mode = "-F" if args.fixed_string else "-E"
        argv = ["sh", "-c", _SEARCH_SCRIPT, "search", args.pattern, args.path,
                str(args.max_results), mode]  # fmt: skip
        out = await run_exec(ctx, argv)
        if isinstance(out, ToolOutput):
            return out
        if timed := _timed_out(out.timed_out, format_exec(out, timeout_s=ctx.tool_timeout_s)):
            return timed
        if out.exit_code == 1:
            return ToolOutput("No matches.", exit_code=1)
        if out.exit_code != 0:
            raise ToolFailed(out.stderr.strip() or f"search failed (exit {out.exit_code})")
        return ToolOutput(out.stdout.rstrip("\n"), exit_code=0)


# -- shell --------------------------------------------------------------------------


class ShellInput(ToolInput):
    argv: Annotated[
        list[Annotated[str, AfterValidator(_no_nul), Field(max_length=4096)]],
        Field(
            min_length=1,
            max_length=64,
            description="Command and arguments, e.g. ['python', 'main.py']. Not a shell string.",
        ),
    ]


class Shell(Tool):
    name = "shell"
    description = (
        "Run one allowlisted command in /workspace (no network). argv is passed "
        "directly, without a shell. Each call starts fresh: no state such as the "
        "current directory carries over, and no process started by the call "
        "survives it."
    )
    input_model = ShellInput
    permission = Permission.EXEC

    async def execute(self, ctx: ToolContext, args: ShellInput) -> ToolOutput:
        program = args.argv[0]
        if "/" in program or program not in ctx.shell_allowlist:
            allowed = ", ".join(sorted(ctx.shell_allowlist)) or "none"
            raise ToolDenied(f"'{program}' is not an allowed command. Allowed: {allowed}.")
        out = await run_exec(ctx, list(args.argv))
        if isinstance(out, ToolOutput):
            return out
        content = format_exec(out, timeout_s=ctx.tool_timeout_s)
        if out.timed_out:
            return ToolOutput(content, ToolStatus.TIMEOUT, out.exit_code)
        # A non-zero exit is information for the agent, not a tool failure.
        return ToolOutput(content, exit_code=out.exit_code)


# -- run_tests ----------------------------------------------------------------------

# First line of a passing run_tests result (trajectory metrics rely on it).
TESTS_PASSED = "Tests passed."


class RunTestsInput(ToolInput):
    pass


class RunTests(Tool):
    name = "run_tests"
    description = "Run the task's test suite and report whether it passed, with its output."
    input_model = RunTestsInput
    permission = Permission.EXEC

    async def execute(self, ctx: ToolContext, args: RunTestsInput) -> ToolOutput:
        if not ctx.test_command:
            raise ToolFailed("this task defines no test command")
        out = await run_exec(ctx, list(ctx.test_command))
        if isinstance(out, ToolOutput):
            return out
        body = format_exec(out, timeout_s=ctx.tool_timeout_s)
        if out.timed_out:
            return ToolOutput(f"Tests timed out.\n{body}", ToolStatus.TIMEOUT, out.exit_code)
        verdict = TESTS_PASSED if out.exit_code == 0 else f"Tests failed (exit {out.exit_code})."
        return ToolOutput(f"{verdict}\n{body}", exit_code=out.exit_code)


BUILTIN_TOOLS: tuple[type[Tool], ...] = (ReadFile, WriteFile, ListDir, Search, Shell, RunTests)
