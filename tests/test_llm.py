from __future__ import annotations

from types import SimpleNamespace

import pytest

from coding_agent.llm import LLMClient, LLMResponseError
from coding_agent.response_parser import ResponseParser
from coding_agent.tools.listfiles import ListFilesTool
from coding_agent.tools.readfile import ReadFileTool


def fake_provider_response():
    message = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="readfile", arguments='{"path": "package.json"}'),
            )
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_llm_client_normalizes_tool_calls(monkeypatch):
    client = LLMClient(api_key="key", model="model", base_url="https://example.test")
    monkeypatch.setattr(client, "_request", lambda messages, tools: fake_provider_response())
    turn = client.complete([], [])
    assert turn.tool_calls[0].name == "readfile"
    assert turn.tool_calls[0].arguments == {"path": "package.json"}


def test_llm_client_keeps_final_content(monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))]
    )
    client = LLMClient(api_key="key", model="model", base_url="https://example.test")
    monkeypatch.setattr(client, "_request", lambda messages, tools: response)
    turn = client.complete([], [])
    assert turn.content == "done"
    assert turn.tool_calls == []


def test_response_parser_classifies_final_response():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="done", tool_calls=[]),
            )
        ]
    )

    parsed = ResponseParser().parse(response)

    assert parsed.kind == "final"
    assert parsed.finish_reason == "stop"
    assert parsed.content == "done"


def test_response_parser_classifies_tool_call_response():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="I will inspect the file.",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="readfile", arguments='{"path":"README.md"}'
                            ),
                        )
                    ],
                ),
            )
        ]
    )

    parsed = ResponseParser().parse(response)

    assert parsed.kind == "tool_call"
    assert parsed.tool_calls[0].arguments == {"path": "README.md"}


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_response_parser_rejects_non_actionable_finish_reason(finish_reason):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content="partial", tool_calls=[]),
            )
        ]
    )

    with pytest.raises(LLMResponseError):
        ResponseParser().parse(response)


def test_response_parser_rejects_inconsistent_tool_call_reason():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=[]),
            )
        ]
    )

    with pytest.raises(LLMResponseError):
        ResponseParser().parse(response)


def test_builtin_tool_schemas_are_valid_registry_definitions():
    from coding_agent.tools.schema import ToolSchemaValidator

    ToolSchemaValidator.validate_definition(ReadFileTool.parameters)
    ToolSchemaValidator.validate_definition(ListFilesTool.parameters)
