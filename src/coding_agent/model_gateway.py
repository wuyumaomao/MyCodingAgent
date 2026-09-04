from __future__ import annotations

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

    def __init__(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        try:
            return self.llm_client.complete(messages, tools)
        except LLMTimeoutError as exc:
            raise ModelGatewayError("timeout", "The model request timed out") from exc
        except InvalidToolArguments as exc:
            raise ModelGatewayError(
                "invalid_tool_arguments", "The model returned invalid tool arguments"
            ) from exc
        except LLMResponseError as exc:
            raise ModelGatewayError(
                "invalid_response", "The model returned an invalid response"
            ) from exc
        except Exception as exc:
            raise ModelGatewayError("provider_error", "The model request failed") from exc
