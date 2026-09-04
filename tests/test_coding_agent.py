from __future__ import annotations

from coding_agent.coding_agent import CodingAgent
from coding_agent.config import Settings
from coding_agent.models import AssistantTurn, ToolCall


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, messages, tools):
        return self.turns.pop(0)


def test_from_settings_builds_reusable_agent_without_trace(monkeypatch, sample_git_repo):
    monkeypatch.setattr(
        "coding_agent.coding_agent.LLMClient",
        lambda **kwargs: FakeLLM([
            AssistantTurn(None, [ToolCall("c1", "listfiles", {})]),
            AssistantTurn("first", []),
            AssistantTurn("second", []),
        ]),
    )
    settings = Settings(api_key="key", model="model")
    agent = CodingAgent.from_settings(sample_git_repo, settings)

    assert agent.ask("inspect") == "first"
    assert agent.ask("again") == "second"
    assert {definition["function"]["name"] for definition in agent.registry.definitions()} == {
        "listfiles", "readfile", "write_file", "patch_file"
    }
