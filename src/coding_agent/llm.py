from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from .models import AssistantTurn, ToolCall


class InvalidToolArguments(ValueError):
    """Raised when a provider returns malformed tool arguments."""


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        return self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        response = self._request(messages, tools)
        message = response.choices[0].message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            arguments = call.function.arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise InvalidToolArguments("Tool arguments are not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise InvalidToolArguments("Tool arguments must be a JSON object")
            tool_calls.append(
                ToolCall(
                    id=str(call.id),
                    name=str(call.function.name),
                    arguments=arguments,
                )
            )
        return AssistantTurn(content=message.content, tool_calls=tool_calls)
