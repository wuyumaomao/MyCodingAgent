from __future__ import annotations

from types import SimpleNamespace

from coding_agent.llm import LLMClient


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
