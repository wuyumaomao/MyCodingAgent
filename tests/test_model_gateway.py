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


def test_model_gateway_retries_retryable_failure_and_reports_attempt():
    turn = AssistantTurn("done", [])

    class Client:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def complete(self, messages, tools):
            self.calls += 1
            self.requests.append((messages, tools))
            if self.calls == 1:
                raise LLMTimeoutError("timed out")
            return turn

    retries = []
    client = Client()
    result = ModelGateway(client, max_retries=1).complete(
        [{"role": "user", "content": "x"}], [], on_retry=retries.append
    )

    assert result is turn
    assert client.calls == 2
    assert client.requests[0] == client.requests[1]
    assert retries[0]["attempt"] == 1
    assert retries[0]["error_type"] == "timeout"
    assert retries[0]["duration_ms"] >= 0


def test_model_gateway_does_not_retry_non_retryable_response_error():
    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            raise LLMResponseError("truncated", retryable=False)

    client = Client()
    with pytest.raises(ModelGatewayError) as exc_info:
        ModelGateway(client, max_retries=1).complete([], [])

    assert client.calls == 1
    assert exc_info.value.error_type == "invalid_response"
