"""The runtime's fixed prompt text. Static on purpose: no timestamps or ids,
so the system prompt + tool list form a stable prompt-cache prefix. Bump
RUNTIME_VERSION whenever this text or the loop's semantics change; it is
recorded in every run_started event."""

RUNTIME_VERSION = "phase3.1"

RUNTIME_PREAMBLE = """\
You are working inside an isolated sandbox.
- Your working directory is /workspace; all file paths are relative to it.
- There is no network access.
- Each tool call starts fresh: processes you start do not survive the call,
  and shell state (current directory, environment) does not carry over.
- When you have finished, reply with your final answer and no tool calls."""


def build_system_prompt(agent_system_prompt: str) -> str:
    agent = agent_system_prompt.strip()
    return f"{agent}\n\n{RUNTIME_PREAMBLE}" if agent else RUNTIME_PREAMBLE


TRUNCATION_NUDGE = "Your previous response was cut off by the output limit. Continue."
TRUNCATED_TOOL_INPUT = (
    "Not run: your response was cut off by the output limit, so this tool "
    "call's input may be incomplete. Issue the call again."
)
