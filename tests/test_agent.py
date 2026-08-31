from __future__ import annotations

from coding_agent.agent import AgentLoop
from coding_agent.context import build_repository_context
from coding_agent.llm import InvalidToolArguments
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.repository import Workspace
from coding_agent.tools.listfiles import ListFilesTool
from coding_agent.tools.readfile import ReadFileTool
from coding_agent.tools.registry import ToolRegistry


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.messages = []

    def complete(self, messages, tools):
        self.calls += 1
        self.messages.append(messages)
        return self.turns.pop(0)


def make_registry(repo):
    workspace = Workspace(repo)
    registry = ToolRegistry()
    listfiles = ListFilesTool(workspace)
    readfile = ReadFileTool(workspace)
    registry.register(listfiles.name, listfiles.execute, listfiles.parameters, description=listfiles.description)
    registry.register(readfile.name, readfile.execute, readfile.parameters, description=readfile.description)
    return registry


def test_loop_executes_tool_and_returns_final_answer(sample_git_repo):
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "package.json"})]),
            AssistantTurn("The repository uses npm scripts.", []),
        ]
    )
    answer = AgentLoop(llm, make_registry(sample_git_repo), build_repository_context, max_rounds=4).run(
        "Explain scripts", Workspace(sample_git_repo)
    )
    assert answer == "The repository uses npm scripts."
    assert llm.calls == 2
    assert any(message.get("role") == "tool" for message in llm.messages[-1])


def test_loop_stops_after_four_tool_rounds(sample_git_repo):
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"call-{n}", "listfiles", {})])
            for n in range(5)
        ]
    )
    answer = AgentLoop(llm, make_registry(sample_git_repo), build_repository_context, max_rounds=4).run(
        "Inspect", Workspace(sample_git_repo)
    )
    assert "4" in answer
    assert llm.calls == 4


def test_loop_returns_tool_error_to_model(sample_git_repo):
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "../secret.txt"})]),
            AssistantTurn("The path is outside the workspace.", []),
        ]
    )
    answer = AgentLoop(llm, make_registry(sample_git_repo), build_repository_context).run(
        "Inspect", Workspace(sample_git_repo)
    )
    assert answer == "The path is outside the workspace."
    assert 'workspace_violation' in llm.messages[-1][-1]["content"]
