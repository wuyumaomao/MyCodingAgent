from __future__ import annotations

from coding_agent.coding_agent import CodingAgent
from coding_agent.config import Settings
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.trace import RunRecorder


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


def test_reused_agent_records_approval_events_in_current_run(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setattr(
        "coding_agent.coding_agent.LLMClient",
        lambda **kwargs: FakeLLM([
            AssistantTurn(None, [ToolCall("w1", "write_file", {"path": "one.txt", "content": "1"})]),
            AssistantTurn("first", []),
            AssistantTurn(None, [ToolCall("w2", "write_file", {"path": "two.txt", "content": "2"})]),
            AssistantTurn("second", []),
        ]),
    )
    settings = Settings(api_key="key", model="model")
    agent = CodingAgent.from_settings(sample_git_repo, settings, approval_ask=lambda _: True)
    first = RunRecorder.create("first", sample_git_repo, tmp_path / "runs")
    second = RunRecorder.create("second", sample_git_repo, tmp_path / "runs")

    assert agent.ask("first", recorder=first) == "first"
    assert agent.ask("second", recorder=second) == "second"

    first_types = [event["type"] for event in first._document["events"]]
    second_events = second._document["events"]
    assert first_types.count("approval_request") == 1
    assert [event["type"] for event in second_events].count("approval_request") == 1
    assert second_events[-1]["type"] == "final_answer"
