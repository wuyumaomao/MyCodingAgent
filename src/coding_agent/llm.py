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
        timeout: float = 600.0,
        provider: str = "openai",
    ) -> None:
        self.model = model
        self.provider = provider
        self.response_parser = ResponseParser()
        # Disable SDK-level retries so the gateway owns retry accounting.
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
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
        #拿到模型原生返回
        response = self._request(messages, tools)
        return self.response_parser.parse(response)

    def complete_text(self, messages: list[dict[str, Any]]) -> str:
        """Run an independent no-tools request and return its text content."""
        response = self._request(messages, [])
        try:
            choice = response.choices[0]
            content = choice.message.content
            finish_reason = getattr(choice, "finish_reason", None)
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMResponseError("The model response did not contain text content") from exc
        if finish_reason not in (None, "stop"):
            raise LLMResponseError(f"The summary response ended with {finish_reason}", retryable=False)
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("The summary response did not contain text content")
        return content
