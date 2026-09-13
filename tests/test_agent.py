from __future__ import annotations

import json

from coding_agent.agent import AgentLimits, AgentLoop
from coding_agent.context import build_repository_context
from coding_agent.llm import InvalidToolArguments
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.repository import Workspace
from coding_agent.tools.listfiles import ListFilesTool
from coding_agent.tools.readfile import ReadFileTool
from coding_agent.tools.approval import WriteApprovalGate
from coding_agent.tools.patchfile import PatchFileTool
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.writefile import WriteFileTool
from coding_agent.trace import RunRecorder


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.messages = []

    def complete(self, messages, tools):
        self.calls += 1
        self.messages.append(messages)
        return self.turns.pop(0)


class RetryingFakeLLM(FakeLLM):
    def complete(self, messages, tools):
        self.calls += 1
        self.messages.append(messages)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def make_registry(repo):
    workspace = Workspace(repo)
    registry = ToolRegistry()
    listfiles = ListFilesTool(workspace)
    readfile = ReadFileTool(workspace)
    registry.register(listfiles.name, listfiles.execute, listfiles.parameters, description=listfiles.description)
    registry.register(readfile.name, readfile.execute, readfile.parameters, description=readfile.description)
    return registry


def make_write_registry(repo, approval_gate):
    workspace = Workspace(repo)
    registry = ToolRegistry()
    for tool in (
        ListFilesTool(workspace),
        ReadFileTool(workspace),
        WriteFileTool(workspace, approval_gate),
        PatchFileTool(workspace, approval_gate),
    ):
        registry.register(tool.name, tool.execute, tool.parameters, description=tool.description)
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


def test_loop_defaults_to_twenty_tool_rounds(sample_git_repo):
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"call-{n}", "listfiles", {})])
            for n in range(20)
        ]
    )
    answer = AgentLoop(llm, make_registry(sample_git_repo)).run(
        "Inspect", Workspace(sample_git_repo)
    )
    assert "20" in answer
    assert llm.calls == 20


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


def test_agent_loop_records_model_and_tool_events(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain scripts", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "package.json"})]),
            AssistantTurn("The repository uses npm scripts.", []),
        ]
    )
    answer = AgentLoop(
        llm,
        make_registry(sample_git_repo),
        build_repository_context,
        recorder=recorder,
    ).run("Explain scripts", Workspace(sample_git_repo))
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    event_types = [event["type"] for event in document["events"]]
    assert answer == "The repository uses npm scripts."
    assert "llm_request" in event_types
    assert "llm_response" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "final_answer" in event_types
    assert event_types[-1] == "final_answer"


def test_agent_report_contains_full_messages_without_debug_flag(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})]),
            AssistantTurn("done", []),
        ]
    )
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "read", Workspace(sample_git_repo)
    )
    events = json.loads(recorder.report_path.read_text(encoding="utf-8"))["events"]
    requests = [event for event in events if event["type"] == "llm_request"]
    responses = [event for event in events if event["type"] == "llm_response"]
    assert requests[0]["messages"][-1]["content"] == "read"
    assert responses[0]["tool_calls"][0]["name"] == "readfile"
    responses = [event for event in events if event["type"] == "llm_response"]
    assert "duration_ms" in responses[0]
    assert all(event["type"] not in {"span_start", "span_end"} for event in events)


def test_loop_retries_llm_api_within_same_round_and_records_metadata(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs")
    llm = RetryingFakeLLM([InvalidToolArguments("bad args"), AssistantTurn("done", [])])

    answer = AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "read", Workspace(sample_git_repo)
    )

    assert answer == "done"
    assert llm.calls == 2
    events = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"]
    retry = next(event for event in events if event["type"] == "llm_retry")
    response = next(event for event in events if event["type"] == "llm_response")
    assert retry["round"] == 1
    assert retry["error_type"] == "invalid_tool_arguments"
    assert response["api_attempts"] == 2
    assert response["round"] == 1


def test_loop_rejects_tool_after_per_tool_limit(sample_git_repo):
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md"})])
            for i in range(4)
        ] + [AssistantTurn("done", [])]
    )
    AgentLoop(
        llm,
        make_registry(sample_git_repo),
        limits=AgentLimits(max_rounds=5, max_calls_per_tool=3),
    ).run("read", Workspace(sample_git_repo))
    assert llm.messages[-1][-1]["role"] == "tool"
    assert "tool_call_limit" in llm.messages[-1][-1]["content"]


def test_loop_accepts_event_sink_and_can_be_reused(sample_git_repo):
    class Sink:
        def __init__(self):
            self.events = []
            self.completed = []
            self.failed = []

        def emit(self, event_type, **payload):
            self.events.append(event_type)

        def complete(self, answer):
            self.completed.append(answer)

        def fail(self, error_type, message, *, duration_ms=None):
            self.failed.append(error_type)

    llm = FakeLLM([AssistantTurn("one", []), AssistantTurn("two", [])])
    sink = Sink()
    loop = AgentLoop(llm, make_registry(sample_git_repo), max_rounds=1)
    assert loop.run("first", Workspace(sample_git_repo), event_sink=sink) == "one"
    assert loop.run("second", Workspace(sample_git_repo), event_sink=sink) == "two"
    assert sink.completed == ["one", "two"]
    assert sink.failed == []


def test_loop_returns_write_approval_error_to_model(sample_git_repo):
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("write-1", "write_file", {"path": "new.py", "content": "ok"})]),
        AssistantTurn("I need approval to write the file.", []),
    ])
    registry = make_write_registry(sample_git_repo, WriteApprovalGate(ask=lambda _: False))

    answer = AgentLoop(llm, registry).run("Create a file", Workspace(sample_git_repo))

    assert answer == "I need approval to write the file."
    assert "approval_denied" in llm.messages[-1][-1]["content"]
    assert not (sample_git_repo / "new.py").exists()


def test_loop_asks_separately_for_multiple_write_calls(sample_git_repo):
    approvals = []
    recorder = RunRecorder.create("write two", sample_git_repo, sample_git_repo / ".runs")
    gate = WriteApprovalGate(ask=lambda preview: approvals.append(preview.path) or True, record=recorder.record)
    llm = FakeLLM([
        AssistantTurn(None, [
            ToolCall("write-1", "write_file", {"path": "one.txt", "content": "1"}),
            ToolCall("write-2", "write_file", {"path": "two.txt", "content": "2"}),
        ]),
        AssistantTurn("Both files were written.", []),
    ])

    answer = AgentLoop(llm, make_write_registry(sample_git_repo, gate), recorder=recorder).run(
        "Write two files", Workspace(sample_git_repo)
    )

    assert answer == "Both files were written."
    assert approvals == ["one.txt", "two.txt"]
    assert (sample_git_repo / "one.txt").read_text(encoding="utf-8") == "1"
    assert (sample_git_repo / "two.txt").read_text(encoding="utf-8") == "2"


def test_loop_rejects_schema_invalid_arguments_before_executor(sample_git_repo):
    calls = []
    registry = ToolRegistry()
    registry.register(
        "readfile",
        lambda arguments: calls.append(arguments) or {"ok": True},
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("bad-1", "readfile", {"path": 123})]),
        AssistantTurn("I corrected the arguments.", []),
    ])

    answer = AgentLoop(llm, registry).run("Read a file", Workspace(sample_git_repo))

    assert answer == "I corrected the arguments."
    assert calls == []
    assert "invalid_tool_arguments" in llm.messages[-1][-1]["content"]


def test_loop_returns_unknown_tool_result(sample_git_repo):
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("unknown-1", "missing_tool", {})]),
        AssistantTurn("That tool is unavailable.", []),
    ])

    answer = AgentLoop(llm, ToolRegistry()).run("Inspect", Workspace(sample_git_repo))

    assert answer == "That tool is unavailable."
    assert "unknown_tool" in llm.messages[-1][-1]["content"]
