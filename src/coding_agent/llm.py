from __future__ import annotations

from typing import Any

from openai import APITimeoutError, OpenAI

from .response_parser import (
    InvalidToolArguments,
    LLMResponseError,
    ParsedResponse,
    ResponseParser,
)


class LLMTimeoutError(TimeoutError):
    """Raised when the model provider exceeds the configured timeout."""


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.response_parser = ResponseParser()
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        try:
            return self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
            )
        except APITimeoutError as exc:
            raise LLMTimeoutError("The model request timed out") from exc

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ParsedResponse:
        response = self._request(messages, tools)
        return self.response_parser.parse(response)
