from __future__ import annotations

import json

import pytest

from coding_agent.agent import AgentError, AgentLimits, AgentLoop
from coding_agent.context import build_repository_context
from coding_agent.llm import LLMResponseError, LLMTimeoutError
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.repository import Workspace
from coding_agent.tools.listfiles import ListFilesTool
from coding_agent.tools.readfile import ReadFileTool
from coding_agent.tools.registry import ToolRegistry
from coding_agent.trace import RunRecorder, TraceWriteError


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, messages, tools):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def make_registry(repo):
    workspace = Workspace(repo)
    registry = ToolRegistry()
    for tool in (ListFilesTool(workspace), ReadFileTool(workspace)):
        registry.register(tool.name, tool.execute, tool.parameters, description=tool.description)
    return registry


def test_trace_event_sequence_for_success(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "README.md"})]),
        AssistantTurn("Done", []),
    ])
    AgentLoop(llm, make_registry(sample_git_repo), build_repository_context, recorder=recorder).run(
        "Explain", Workspace(sample_git_repo)
    )
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    event_types = [event["type"] for event in document["events"]]
    assert event_types[0] == "run_started"
    assert event_types.count("llm_request") == 2
    assert event_types.count("llm_response") == 2
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "final_answer" in event_types
    assert event_types[-1] == "final_answer"
    assert [event["seq"] for event in document["events"]] == list(
        range(1, len(document["events"]) + 1)
    )


def test_trace_records_provider_failure(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([RuntimeError("provider down")])
    with pytest.raises(AgentError):
        AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
            "Explain", Workspace(sample_git_repo)
        )
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert any(event["type"] == "run_failed" for event in document["events"])
    assert document["events"][-1]["type"] == "run_failed"


def test_trace_records_invalid_model_response(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([LLMResponseError("invalid finish reason")])

    with pytest.raises(AgentError):
        AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
            "Explain", Workspace(sample_git_repo)
        )

    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    failures = [event for event in document["events"] if event["type"] == "run_failed"]
    assert failures[-1]["error_type"] == "invalid_response"


def test_trace_records_timeout_as_timeout(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([LLMTimeoutError("timed out")])
    with pytest.raises(AgentError):
        AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
            "Explain", Workspace(sample_git_repo)
        )
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    failures = [event for event in document["events"] if event["type"] == "run_failed"]
    assert failures[-1]["error_type"] == "timeout"


def test_trace_redacts_secret_values(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    recorder.record("debug", api_key="super-secret")
    text = recorder.trace_path.read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "[REDACTED]" in text


def test_trace_write_failure_keeps_previous_json(sample_git_repo, tmp_path, monkeypatch):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    previous = recorder.trace_path.read_text(encoding="utf-8")

    def fail_replace(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(type(recorder.trace_path), "replace", fail_replace)
    with pytest.raises(TraceWriteError):
        recorder.record("tool_call", name="readfile")
    assert recorder.trace_path.read_text(encoding="utf-8") == previous


def test_trace_records_step_durations(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([AssistantTurn("Done", [])])
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "Explain", Workspace(sample_git_repo)
    )
    events = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"]
    assert all(event["type"] not in {"span_start", "span_end"} for event in events)
    assert all(
        "duration_ms" in event
        for event in events
        if event["type"] in {"llm_response", "tool_result"}
    )


def test_trace_records_tool_call_limit(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md"})])
            for i in range(4)
        ] + [AssistantTurn("done", [])]
    )
    AgentLoop(
        llm,
        make_registry(sample_git_repo),
        recorder=recorder,
        limits=AgentLimits(max_rounds=5, max_calls_per_tool=3),
    ).run("read", Workspace(sample_git_repo))
    trace = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert any(event["type"] == "tool_call_limit" for event in trace["events"])


def test_trace_and_report_share_event_sequence(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([AssistantTurn("Done", [])])
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "Explain", Workspace(sample_git_repo)
    )
    trace = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    report = json.loads(recorder.report_path.read_text(encoding="utf-8"))
    assert [event["seq"] for event in trace["events"]] == [event["seq"] for event in report["events"]]
    trace_request = next(event for event in trace["events"] if event["type"] == "llm_request")
    report_request = next(event for event in report["events"] if event["type"] == "llm_request")
    assert "messages" not in trace_request
    assert "messages" in report_request
