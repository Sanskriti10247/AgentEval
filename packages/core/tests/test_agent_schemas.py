import pytest
from pydantic import ValidationError

from agenteval_core.schemas.agent import AgentCreate, AgentVersionCreate
from agenteval_core.schemas.task import TaskCreate
from agenteval_core.services.agents import config_hash_of

AGENT = {"name": "t", "task_type": "agent", "image": "i", "instructions": "do it"}
COMMAND = {"name": "t", "image": "i", "command": ["true"]}


def test_agent_task_defaults_and_limits() -> None:
    t = TaskCreate.model_validate({**AGENT, "timeout_s": 1800})
    assert (t.command, t.test_command, t.timeout_s) == (None, None, 1800)


@pytest.mark.parametrize(
    "body",
    [
        {**AGENT, "instructions": None},
        {**AGENT, "command": ["x"]},
        {**AGENT, "timeout_s": 1801},
        {**AGENT, "test_command": []},
        {**AGENT, "test_command": ["py", "a\x00b"]},
        {**COMMAND, "instructions": "x"},
        {**COMMAND, "test_command": ["pytest"]},
        {**COMMAND, "timeout_s": 301},
        {"name": "t", "image": "i"},  # command task without a command
    ],
)
def test_task_rules_by_type(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(body)


def test_command_tasks_are_unchanged() -> None:
    t = TaskCreate.model_validate(COMMAND)
    assert (t.task_type, t.timeout_s, t.instructions) == ("command", 30, None)


@pytest.mark.parametrize("name", ["fixer", "fixer-v2", "a.b_c", "9lives"])
def test_valid_agent_names(name: str) -> None:
    assert AgentCreate(name=name).name == name


@pytest.mark.parametrize("name", ["", "-x", "has space", "a/b", "x" * 201])
def test_invalid_agent_names(name: str) -> None:
    with pytest.raises(ValidationError):
        AgentCreate(name=name)


@pytest.mark.parametrize("program", ["/bin/sh", "rm -rf", "", "../x", "a;b"])
def test_shell_allowlist_must_be_bare_program_names(program: str) -> None:
    with pytest.raises(ValidationError):
        AgentVersionCreate(model_provider="fake", model="m", tools=["shell"],
                           shell_allowlist=[program])  # fmt: skip


def test_config_hash_is_canonical() -> None:
    a = {"model": "m", "tools": ["a"], "limits": {"x": 1, "y": 2}}
    b = {"limits": {"y": 2, "x": 1}, "tools": ["a"], "model": "m"}
    assert config_hash_of(a) == config_hash_of(b)
    assert config_hash_of(a) != config_hash_of({**a, "model": "n"})
    assert len(config_hash_of(a)) == 64
