from __future__ import annotations

import json

from coding_agent.agent import AgentLimits, AgentLoop
from coding_agent.context import build_repository_context
from coding_agent.llm import InvalidToolArguments
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.repository import Workspace
from coding_agent.session import SessionStore
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
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    answer = AgentLoop(llm, make_registry(sample_git_repo), build_repository_context, max_rounds=4, session=session, session_store=store).run(
        "Explain scripts", workspace
    )
    assert answer == "The repository uses npm scripts."
    assert llm.calls == 2
    assert any(message.get("role") == "tool" for message in llm.messages[-1])
    assert store.load(session.session_id).run_state["status"] == "completed"


def test_loop_marks_run_cancelled_when_model_is_interrupted(sample_git_repo):
    class InterruptingLLM:
        def complete(self, messages, tools):
            raise KeyboardInterrupt

    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    loop = AgentLoop(InterruptingLLM(), make_registry(sample_git_repo), session=session, session_store=store)
    with __import__("pytest").raises(KeyboardInterrupt):
        loop.run("Inspect", workspace)
    loaded = store.load(session.session_id)
    assert loaded.run_state["status"] == "cancelled"
    assert loaded.run_state["reason"] == "keyboard_interrupt"
    assert loaded.cancelled_runs == [{"run_id": loaded.run_state["run_id"], "start": 0, "end": 2}]


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


def test_loop_rejects_identical_repeated_read(sample_git_repo):
    """重复检测取代了按工具计数：同一个调用第二次就被拒绝。"""
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md"})])
            for i in range(4)
        ] + [AssistantTurn("done", [])]
    )
    AgentLoop(
        llm,
        make_registry(sample_git_repo),
        limits=AgentLimits(max_rounds=5),
    ).run("read", Workspace(sample_git_repo))
    assert llm.messages[-1][-1]["role"] == "tool"
    assert "repeated_tool_call" in llm.messages[-1][-1]["content"]


def test_loop_allows_many_distinct_reads(sample_git_repo):
    """读同一个文件的不同区间不算重复，不该被拦。"""
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md", "start": i + 1, "end": i + 1})])
            for i in range(4)
        ] + [AssistantTurn("done", [])]
    )
    AgentLoop(
        llm,
        make_registry(sample_git_repo),
        limits=AgentLimits(max_rounds=5),
    ).run("read", Workspace(sample_git_repo))
    assert "repeated_tool_call" not in llm.messages[-1][-1]["content"]
    assert llm.messages[-1][-1]["role"] == "tool"


def test_loop_safety_valve_still_stops_runaway_calls(sample_git_repo):
    """计数上限退化成安全阀，但仍能兜住异常行为。"""
    llm = FakeLLM(
        [
            AssistantTurn(None, [ToolCall(f"c{i}", "listfiles", {"path": f"dir{i}"})])
            for i in range(4)
        ] + [AssistantTurn("done", [])]
    )
    AgentLoop(
        llm,
        make_registry(sample_git_repo),
        limits=AgentLimits(max_rounds=5, max_calls_per_tool=2),
    ).run("read", Workspace(sample_git_repo))
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


def test_checkpoint_is_saved_before_tool_execution_crash(sample_git_repo, monkeypatch):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    llm = FakeLLM([AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})])])

    def crash(_executor, _call):
        raise RuntimeError("simulated process failure")

    monkeypatch.setattr("coding_agent.agent.ToolExecutor.execute", crash)
    loop = AgentLoop(llm, make_registry(sample_git_repo), session=session, session_store=store)

    with __import__("pytest").raises(RuntimeError, match="simulated"):
        loop.run("read README", workspace)

    loaded = store.load(session.session_id)
    active = loaded.checkpoints["active"]
    assert active["status"] == "running"
    assert active["calls"][0]["id"] == "c1"
    assert loaded.history[-1]["role"] == "assistant"


def test_run_reconciles_interrupted_write_before_next_model_request(sample_git_repo):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    (sample_git_repo / "result.txt").write_text("done\n", encoding="utf-8")
    call = ToolCall("w1", "write_file", {"path": "result.txt", "content": "done\n"})
    session.history.extend([
        {"role": "user", "content": "write result"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "w1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps(call.arguments)}}]},
    ])
    session.checkpoints["active"] = {
        "id": "cp-1",
        "run_id": "old-run",
        "round": 1,
        "status": "running",
        "calls": [{"id": "w1", "name": "write_file", "arguments": call.arguments, "status": "running", "workspace_before": {"files": {"result.txt": {"exists": False}}}}],
    }
    store.save(session)

    llm = FakeLLM([AssistantTurn("resumed", [])])
    loop = AgentLoop(llm, make_write_registry(sample_git_repo, WriteApprovalGate(ask=lambda _: True)), session=session, session_store=store)

    assert loop.run("continue", workspace) == "resumed"
    assert any(
        message.get("role") == "tool" and "reconciled" in message.get("content", "")
        for message in llm.messages[0]
    )
    assert store.load(session.session_id).checkpoints["active"] is None


def test_recovery_refuses_to_resume_after_runtime_identity_change(sample_git_repo):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    session.runtime_identity["model"] = "old-model"
    session.history.extend([
        {"role": "user", "content": "read README"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "r1", "type": "function",
            "function": {"name": "readfile", "arguments": json.dumps({"path": "README.md"})},
        }]},
    ])
    session.checkpoints["active"] = {
        "id": "cp-runtime", "run_id": "old-run", "round": 1, "status": "running",
        "calls": [{"id": "r1", "name": "readfile", "arguments": {"path": "README.md"}, "status": "running"}],
    }
    store.save(session)

    llm = FakeLLM([AssistantTurn("continued", [])])
    loop = AgentLoop(
        llm,
        make_registry(sample_git_repo),
        session=session,
        session_store=store,
        runtime_identity={"model": "new-model"},
    )

    assert loop.run("continue", workspace) == "continued"
    loaded = store.load(session.session_id)
    assert loaded.resume_state["status"] == "runtime_mismatch"
    assert loaded.resume_state["runtime_mismatches"] == ["model"]
    assert "runtime identity changed" in llm.messages[0][-1]["content"]


def test_recovery_does_not_duplicate_tool_result_already_in_history(sample_git_repo):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    arguments = {"path": "README.md"}
    existing_result = {"role": "tool", "tool_call_id": "r2", "content": json.dumps({"ok": True, "path": "README.md"})}
    session.history.extend([
        {"role": "user", "content": "read README"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "r2", "type": "function",
            "function": {"name": "readfile", "arguments": json.dumps(arguments)},
        }]},
        existing_result,
    ])
    session.checkpoints["active"] = {
        "id": "cp-duplicate", "run_id": "old-run", "round": 1, "status": "running",
        "calls": [{"id": "r2", "name": "readfile", "arguments": arguments, "status": "running"}],
    }
    store.save(session)

    llm = FakeLLM([AssistantTurn("continued", [])])
    loop = AgentLoop(llm, make_registry(sample_git_repo), session=session, session_store=store)

    assert loop.run("continue", workspace) == "continued"
    assert [m for m in llm.messages[0] if m.get("role") == "tool" and m.get("tool_call_id") == "r2"] == [
        existing_result
    ]


def test_new_query_excludes_cancelled_run_history(sample_git_repo):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    session = store.create()
    session.history.extend([
        {"role": "user", "content": "wrong command A"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "patch_file", "arguments": json.dumps({"path": "x.py", "old_text": "a", "new_text": "b"})},
        }]},
        {"role": "tool", "tool_call_id": "p1", "content": json.dumps({
            "ok": False, "error": {"type": "execution_interrupted"},
        })},
    ])
    session.checkpoints["active"] = {
        "id": "cp-cancelled", "run_id": "old-run", "round": 1, "status": "cancelled",
        "calls": [{"id": "p1", "name": "patch_file", "arguments": {"path": "x.py", "old_text": "a", "new_text": "b"}, "status": "interrupted"}],
    }
    session.run_state = {"status": "cancelled", "run_id": "old-run", "query": "wrong command A", "round": 1, "reason": "keyboard_interrupt"}
    session.cancelled_runs = [{"run_id": "old-run", "start": 0, "end": 3}]
    store.save(session)

    llm = FakeLLM([AssistantTurn("new task done", [])])
    loop = AgentLoop(llm, make_registry(sample_git_repo), session=session, session_store=store)

    assert loop.run("new command B", workspace) == "new task done"
    prompt_text = json.dumps(llm.messages[0], ensure_ascii=False)
    assert "wrong command A" not in prompt_text
    assert '"p1"' not in prompt_text
    assert "x.py" not in prompt_text
    assert "new command B" in prompt_text
