from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from .models import AssistantTurn, ToolCall


class InvalidToolArguments(ValueError):
    """Raised when a provider returns malformed tool arguments."""


class LLMResponseError(RuntimeError):
    """Raised when a provider response cannot be classified safely."""


@dataclass(frozen=True)
class ParsedResponse(AssistantTurn):#ParsedResponse继承AssistantTurn
    kind: Literal["final", "tool_call"] = "final"
    finish_reason: str = "stop"


class ResponseParser:
    """Validate and normalize a Chat Completions response."""

    def parse(self, response: Any) -> ParsedResponse:
        try:
            choice = response.choices[0]
            message = choice.message
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise LLMResponseError("The model response did not contain a choice") from exc

        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [self._parse_tool_call(call) for call in raw_tool_calls]
        finish_reason = getattr(choice, "finish_reason", None)#去拿finishreason
        if finish_reason is None:#没有finishreason且没有toolcall就是结束
            # Some OpenAI-compatible providers omit this metadata.
            finish_reason = "tool_calls" if tool_calls else "stop"

        if finish_reason in {"length", "content_filter"}:
            raise LLMResponseError(f"The model response ended with {finish_reason}")
        if finish_reason not in {"stop", "tool_calls", "function_call"}:
            raise LLMResponseError(f"Unknown finish_reason: {finish_reason}")
        if finish_reason in {"tool_calls", "function_call"} and not tool_calls:
            raise LLMResponseError("finish_reason indicates tool calls, but none were returned")
        if finish_reason == "stop" and tool_calls:
            raise LLMResponseError("finish_reason is stop, but tool calls were returned")
        #模型拿到的返回结构化结果
        return ParsedResponse(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
            kind="tool_call" if tool_calls else "final",
            finish_reason=str(finish_reason),
        )

    @staticmethod
    def _parse_tool_call(call: Any) -> ToolCall:
        try:
            call_id = str(call.id)
            function = call.function
            name = str(function.name)
            arguments: Any = function.arguments
        except (AttributeError, TypeError) as exc:
            raise LLMResponseError("The model returned an invalid tool call") from exc

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise InvalidToolArguments("Tool arguments are not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise InvalidToolArguments("Tool arguments must be a JSON object")
        return ToolCall(id=call_id, name=name, arguments=arguments)
