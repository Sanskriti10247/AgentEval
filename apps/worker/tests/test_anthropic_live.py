"""OPT-IN live smoke test against the real Anthropic API. Costs money.

Never part of the standard suite. It runs only when both are set:

    AGENTEVAL_LIVE_ANTHROPIC=1  ANTHROPIC_API_KEY=sk-ant-...
    uv run pytest apps/worker/tests/test_anthropic_live.py -m live

One small request: checks that the real API accepts our request shape
(strict, strict-compatible tool schemas; adaptive thinking; effort; cache
control) and that the response translates. Expected cost: well under $0.01.
"""

import os

import pytest

from agenteval_core.providers.base import StopReason
from agenteval_core.tools.registry import REGISTRY, ToolSet

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGENTEVAL_LIVE_ANTHROPIC") != "1"
        or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="opt-in: set AGENTEVAL_LIVE_ANTHROPIC=1 and ANTHROPIC_API_KEY",
    ),
]


async def test_one_real_request_round_trips() -> None:
    from agenteval_worker.providers.anthropic import AnthropicProvider, make_client

    provider = AnthropicProvider(make_client(os.environ["ANTHROPIC_API_KEY"]))
    session = provider.start(
        model="claude-opus-5",
        system="You are terse.",
        tools=ToolSet.build(sorted(REGISTRY), has_test_command=True).specs(),
        task_prompt="Reply with the single word: ok. Do not use any tools.",
        settings={"effort": "low"},
    )
    turn = await session.next(max_tokens=256, timeout_s=60)
    assert turn.stop in (StopReason.END_TURN, StopReason.TOOL_USE)
    assert turn.usage.input_tokens > 0 and turn.model.startswith("claude-opus-5")
