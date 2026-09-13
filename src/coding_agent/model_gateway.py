from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from .llm import InvalidToolArguments, LLMResponseError, LLMTimeoutError
from .models import AssistantTurn


class ModelGatewayError(RuntimeError):
    """Stable model error exposed to the agent loop."""

    def __init__(self, error_type: str, public_message: str) -> None:
        super().__init__(public_message)
        self.error_type = error_type
        self.public_message = public_message


class ModelGateway:
    """Call an LLM client and normalize provider failures."""

    def __init__(self, llm_client: Any, max_retries: int = 1) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.llm_client = llm_client
        self.max_retries = max_retries

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_retry: Callable[[dict[str, object]], None] | None = None,
    ) -> AssistantTurn:
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                return self.llm_client.complete(messages, tools)
            except Exception as exc:
                retryable = self._is_retryable(exc)
                if retryable and attempt < self.max_retries:
                    if on_retry is not None:
                        on_retry(
                            {
                                "attempt": attempt + 1,
                                "error_type": self._error_type(exc),
                                "duration_ms": self._duration_ms(started),
                            }
                        )
                    continue
                raise self._normalize(exc) from exc

        raise AssertionError("unreachable")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, LLMResponseError):
            return exc.retryable
        return isinstance(exc, (LLMTimeoutError, InvalidToolArguments))

    @staticmethod
    def _error_type(exc: Exception) -> str:
        if isinstance(exc, LLMTimeoutError):
            return "timeout"
        if isinstance(exc, InvalidToolArguments):
            return "invalid_tool_arguments"
        if isinstance(exc, LLMResponseError):
            return "invalid_response"
        return "provider_error"

    @classmethod
    def _normalize(cls, exc: Exception) -> ModelGatewayError:
        error_type = cls._error_type(exc)
        messages = {
            "timeout": "The model request timed out",
            "invalid_tool_arguments": "The model returned invalid tool arguments",
            "invalid_response": "The model returned an invalid response",
            "provider_error": "The model request failed",
        }
        return ModelGatewayError(error_type, messages[error_type])

    @staticmethod
    def _duration_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 3)
