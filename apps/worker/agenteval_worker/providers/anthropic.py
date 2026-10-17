"""AnthropicProvider: the Messages API behind the neutral ModelProvider.

Request shape (nothing else is configurable per agent version):

  model, max_tokens, timeout      from the runtime / agent version
  system                          one text block with a cache breakpoint
  tools                           strict: true, schemas made strict-compatible
  cache_control                   top-level automatic caching for the growing tail
  thinking                        {"type": "adaptive"} where the model supports it
  output_config.effort            from the version's (normalised) settings

Deliberately absent: SDK retries (max_retries=0; the runtime retries and
records each try), refusal fallbacks (benchmark results must stay
attributable to the configured model), forced tool_choice, betas, streaming.

The transcript is SDK-typed and append-only. Assistant content is appended
exactly as returned, so thinking blocks are replayed verbatim; they are never
logged or persisted (the runtime only sees text, tool calls and usage).
"""

from __future__ import annotations

import copy
import time
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolResultBlockParam

from agenteval_core.providers.anthropic_settings import ANTHROPIC_MODELS, AnthropicSettings
from agenteval_core.providers.base import (
    ModelError,
    ModelErrorKind,
    ModelTurn,
    PendingToolCalls,
    StopReason,
    ToolCall,
    ToolResultMsg,
    ToolSpec,
    Usage,
)

EMPTY_TOOL_RESULT = "(empty)"

_STOP = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}

# Schema keywords strict tool use does not support. They are removed from
# what the model sees and described in words instead; ToolSet.invoke still
# enforces them with the full pydantic model.
_UNSUPPORTED = {
    "minimum": "value >= {}",
    "maximum": "value <= {}",
    "exclusiveMinimum": "value > {}",
    "exclusiveMaximum": "value < {}",
    "multipleOf": "multiple of {}",
    "minLength": "at least {} characters",
    "maxLength": "at most {} characters",
    "minItems": "at least {} items",
    "maxItems": "at most {} items",
    "uniqueItems": "items unique: {}",
}


def make_client(api_key: str) -> AsyncAnthropic:
    """The only way the worker builds a client: SDK retries are always off."""
    return AsyncAnthropic(api_key=api_key, max_retries=0)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: AsyncAnthropic) -> None:
        if client.max_retries != 0:
            raise ValueError("SDK retries must be disabled (max_retries=0); the runtime retries")
        self._client = client

    def start(
        self,
        *,
        model: str,
        system: str,
        tools: list[ToolSpec],
        task_prompt: str,
        settings: dict[str, Any],
        attempt_no: int = 1,  # unused: a real model does not vary by attempt
    ) -> AnthropicSession:
        caps = ANTHROPIC_MODELS.get(model)
        if caps is None:
            raise ValueError(f"unsupported Anthropic model {model!r}")
        parsed = AnthropicSettings.model_validate(settings)
        base: dict[str, Any] = {
            "model": model,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": [tool_definition(t) for t in tools],
            "cache_control": {"type": "ephemeral"},
        }
        if caps.adaptive_thinking:
            base["thinking"] = {"type": "adaptive"}
        if caps.effort and parsed.effort:
            base["output_config"] = {"effort": parsed.effort}
        return AnthropicSession(self._client, base, task_prompt)


class AnthropicSession:
    def __init__(self, client: AsyncAnthropic, base: dict[str, Any], task_prompt: str) -> None:
        self._client = client
        self._base = base
        self.messages: list[MessageParam] = [{"role": "user", "content": task_prompt}]
        self._pending = PendingToolCalls()

    async def next(self, *, max_tokens: int, timeout_s: float) -> ModelTurn:
        self._pending.check_can_call_model()
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                **self._base, messages=self.messages, max_tokens=max_tokens, timeout=timeout_s
            )
        except anthropic.APIError as exc:
            raise translate_error(exc) from exc
        except anthropic.AnthropicError as exc:  # e.g. missing credentials
            raise ModelError(ModelErrorKind.AUTHENTICATION, str(exc)) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        calls = tuple(
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        )
        text = "\n\n".join(b.text for b in response.content if b.type == "text")
        # Verbatim, including thinking blocks: never edit what was returned.
        self.messages.append({"role": "assistant", "content": response.content})
        if calls:
            self._pending.expect(calls)

        stop = _STOP.get(response.stop_reason or "", StopReason.OTHER)
        detail: str | None = None
        if stop is StopReason.REFUSAL:
            details = getattr(response, "stop_details", None)
            detail = getattr(details, "category", None)
        elif stop is StopReason.OTHER:
            detail = response.stop_reason
        u = response.usage
        return ModelTurn(
            text=text,
            tool_calls=calls,
            stop=stop,
            stop_detail=detail,
            usage=Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_input_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
            ),
            model=response.model,
            latency_ms=latency_ms,
            request_id=getattr(response, "_request_id", None),
        )

    def add_tool_results(self, results: list[ToolResultMsg]) -> None:
        self._pending.resolve(results)
        blocks: list[ToolResultBlockParam] = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content or EMPTY_TOOL_RESULT,
                "is_error": r.is_error,
            }
            for r in results
        ]
        self.messages.append({"role": "user", "content": blocks})

    def add_user_text(self, text: str) -> None:
        self._pending.check_can_add_user_text()
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})


# -- tools --------------------------------------------------------------------------------


def tool_definition(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": strict_schema(spec.input_schema),
        "strict": True,
    }


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of `schema` without keywords strict tool use rejects; each
    removed constraint is appended to that node's description. Objects keep
    additionalProperties: false (ToolInput guarantees it)."""
    node = copy.deepcopy(schema)
    _strip(node)
    return node


def _strip(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _strip(item)
        return
    if not isinstance(node, dict):
        return
    # Fixed order (min before max) so descriptions are stable and readable.
    removed = [_UNSUPPORTED[k].format(node.pop(k)) for k in _UNSUPPORTED if k in node]
    if removed:
        note = f"({', '.join(removed)})"
        node["description"] = f"{node['description']} {note}" if node.get("description") else note
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            for prop in value.values():  # property names are data, not keywords
                _strip(prop)
        else:
            _strip(value)


# -- errors --------------------------------------------------------------------------------


def translate_error(exc: anthropic.APIError) -> ModelError:
    """Map SDK errors onto ModelErrorKind. Retryability is decided by the
    kind (RETRYABLE_KINDS), never here."""
    if isinstance(exc, anthropic.APITimeoutError):  # before APIConnectionError: a subclass
        return ModelError(ModelErrorKind.TIMEOUT, str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return ModelError(ModelErrorKind.CONNECTION, str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        return ModelError(
            _kind_for_status(status),
            exc.message,
            status=status,
            retry_after_s=_retry_after(exc),
            request_id=exc.response.headers.get("request-id"),
        )
    return ModelError(ModelErrorKind.SERVER_ERROR, str(exc))  # e.g. malformed response


def _kind_for_status(status: int) -> ModelErrorKind:
    if status == 429:
        return ModelErrorKind.RATE_LIMITED
    if status == 529:
        return ModelErrorKind.OVERLOADED
    if status in (408, 504):
        return ModelErrorKind.TIMEOUT
    if status >= 500:
        return ModelErrorKind.SERVER_ERROR
    return {
        401: ModelErrorKind.AUTHENTICATION,
        402: ModelErrorKind.PERMISSION,  # billing: an account problem, not retryable
        403: ModelErrorKind.PERMISSION,
        404: ModelErrorKind.NOT_FOUND,
        409: ModelErrorKind.CONFLICT,
        413: ModelErrorKind.REQUEST_TOO_LARGE,
    }.get(status, ModelErrorKind.BAD_REQUEST)


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    headers = exc.response.headers
    if ms := headers.get("retry-after-ms"):
        try:
            return float(ms) / 1000
        except ValueError:
            pass
    if value := headers.get("retry-after"):
        try:
            return float(value)
        except ValueError:
            return None  # an HTTP date; the runtime's own backoff applies
    return None
