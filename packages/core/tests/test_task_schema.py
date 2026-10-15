import pytest
from pydantic import ValidationError

from agenteval_core.schemas.task import MAX_FILES_BYTES, TaskCreate

BASE = {"name": "t", "image": "agenteval-sandbox:py312", "command": ["python", "main.py"]}


def make(**overrides: object) -> TaskCreate:
    return TaskCreate.model_validate({**BASE, **overrides})


def test_defaults() -> None:
    t = make()
    assert (t.timeout_s, t.memory_mb, t.cpu_limit, t.files, t.task_type) == (
        30,
        256,
        1.0,
        {},
        "command",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 0),
        ("timeout_s", 301),
        ("memory_mb", 63),
        ("memory_mb", 1025),
        ("cpu_limit", 0.05),
        ("cpu_limit", 2.5),
        ("command", []),
        ("command", [""]),
        ("command", ["a\x00b"]),
        ("name", ""),
        ("task_type", "agent"),
    ],
)
def test_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make(privileged=True)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../escape.py",
        "a/../../b",
        "a/./b",
        "a//b",
        "a/",
        "",
        ".",
        "..",
        "a\\b",
        "x\x00y",
        "a" * 256,
    ],
)
def test_bad_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        make(files={path: "x"})


@pytest.mark.parametrize("path", ["main.py", "pkg/mod.py", "a/b/c/.hidden", "..dots-in-name"])
def test_good_paths_accepted(path: str) -> None:
    assert path in make(files={path: "x"}).files


def test_total_file_size_capped() -> None:
    make(files={"a.txt": "x" * (MAX_FILES_BYTES - 100)})
    with pytest.raises(ValidationError):
        make(files={"a.txt": "x" * MAX_FILES_BYTES})
    with pytest.raises(ValidationError):
        make(files={"a.txt": "é" * (MAX_FILES_BYTES // 2)})  # counted in UTF-8 bytes
