"""Task limits and validation helpers shared by the task API schemas
(schemas/task_api.py) and the evaluation spec (evaluation/spec.py)."""

import posixpath

MAX_FILES_BYTES = 1024 * 1024
MAX_FILES = 100
MAX_COMMAND_ARGS = 64
MAX_ARG_LEN = 4096
MAX_COMMAND_TIMEOUT_S = 300
MAX_AGENT_TIMEOUT_S = 1800


def validate_argv(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if not v or not v[0]:
        raise ValueError("argv must be non-empty and argv[0] must not be empty")
    if len(v) > MAX_COMMAND_ARGS:
        raise ValueError(f"at most {MAX_COMMAND_ARGS} args")
    for arg in v:
        if len(arg) > MAX_ARG_LEN or "\x00" in arg:
            raise ValueError(f"args must be <= {MAX_ARG_LEN} chars with no NUL")
    return v


def validate_relative_path(path: str) -> None:
    """Files are extracted under /workspace; paths must stay inside it."""
    if not path or len(path) > 255 or "\x00" in path or "\\" in path:
        raise ValueError(f"invalid file path: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"file path must be relative: {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts) or posixpath.normpath(path) != path:
        raise ValueError(f"file path must be normalized with no '..': {path!r}")
