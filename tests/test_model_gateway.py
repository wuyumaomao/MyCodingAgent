from __future__ import annotations

import pytest

from coding_agent.llm import InvalidToolArguments, LLMResponseError, LLMTimeoutError
from coding_agent.model_gateway import ModelGateway, ModelGatewayError
from coding_agent.models import AssistantTurn


@pytest.mark.parametrize(
    ("error", "error_type", "message"),
    [
        (LLMTimeoutError("provider timeout"), "timeout", "The model request timed out"),
        (
            InvalidToolArguments("bad args"),
            "invalid_tool_arguments",
            "The model returned invalid tool arguments",
        ),
        (
            LLMResponseError("bad response"),
            "invalid_response",
            "The model returned an invalid response",
        ),
        (RuntimeError("provider down"), "provider_error", "The model request failed"),
    ],
)
def test_model_gateway_normalizes_provider_errors(error, error_type, message):
    class FailingClient:
        def complete(self, messages, tools):
            raise error

    with pytest.raises(ModelGatewayError) as exc_info:
        ModelGateway(FailingClient()).complete([], [])

    assert exc_info.value.error_type == error_type
    assert exc_info.value.public_message == message
    assert exc_info.value.__cause__ is error


def test_model_gateway_returns_turn_unchanged():
    turn = AssistantTurn("done", [])

    class Client:
        def complete(self, messages, tools):
            return turn

    assert ModelGateway(Client()).complete([], []) is turn
