from __future__ import annotations

from coding_agent.agent import AgentLimits
from coding_agent.models import ToolCall
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools.registry import ToolRegistry


class Events:
    def __init__(self):
        self.items = []

    def emit(self, event_type, **payload):
        self.items.append((event_type, payload))


def registry_for(executor):
    registry = ToolRegistry()
    registry.register("echo", executor, {"type": "object", "additionalProperties": True})
    return registry


def test_tool_executor_validates_and_returns_result():
    events = Events()
    registry = registry_for(lambda arguments: {"ok": True, "value": arguments["value"]})
    registry._schemas["echo"] = {"type": "object", "required": ["value"]}
    execution = ToolExecutor(registry, AgentLimits(), events).execute(
        ToolCall("c1", "echo", {"value": "ok"})
    )
    assert execution.result == {"ok": True, "value": "ok"}
    assert execution.duration_ms >= 0
    assert [item[0] for item in events.items] == ["tool_call", "tool_result"]


def test_tool_executor_returns_schema_error_without_executing():
    calls = []
    events = Events()
    registry = registry_for(lambda arguments: calls.append(arguments) or {"ok": True})
    registry._schemas["echo"] = {"type": "object", "required": ["value"]}
    execution = ToolExecutor(registry, AgentLimits(), events).execute(
        ToolCall("c1", "echo", {})
    )
    assert execution.result["error"]["type"] == "invalid_tool_arguments"
    assert calls == []


def test_tool_executor_enforces_limit_per_instance():
    events = Events()
    registry = registry_for(lambda arguments: {"ok": True})
    executor = ToolExecutor(
        registry, AgentLimits(max_calls_per_tool=1), events
    )
    call = ToolCall("c1", "echo", {})
    assert executor.execute(call).result["ok"] is True
    assert executor.execute(ToolCall("c2", "echo", {})).result["error"]["type"] == "tool_call_limit"
    fresh = ToolExecutor(registry, AgentLimits(max_calls_per_tool=1), Events())
    assert fresh.execute(ToolCall("c3", "echo", {})).result["ok"] is True


def readonly_registry(result):
    registry = ToolRegistry()
    registry.register("readfile", lambda arguments: result, {"type": "object", "additionalProperties": True})
    return registry


def test_identical_successful_read_is_rejected():
    events = Events()
    executor = ToolExecutor(readonly_registry({"ok": True, "path": "a.py"}), AgentLimits(), events)
    call = ToolCall("c1", "readfile", {"path": "a.py"})

    assert executor.execute(call).result["ok"] is True

    repeat = executor.execute(ToolCall("c2", "readfile", {"path": "a.py"})).result
    assert repeat["error"]["type"] == "repeated_tool_call"
    assert repeat["error"]["reason"] == "already_succeeded"
    assert "tool_call_repeat" in [item[0] for item in events.items]


def test_different_arguments_are_not_repeats():
    executor = ToolExecutor(readonly_registry({"ok": True}), AgentLimits(), Events())

    assert executor.execute(ToolCall("c1", "readfile", {"path": "a.py", "start": 1, "end": 20})).result["ok"] is True
    assert executor.execute(ToolCall("c2", "readfile", {"path": "a.py", "start": 21, "end": 40})).result["ok"] is True
    assert executor.execute(ToolCall("c3", "readfile", {"path": "b.py"})).result["ok"] is True


def test_identical_failure_is_rejected_after_one_retry():
    executor = ToolExecutor(readonly_registry({"ok": False, "error": {"type": "file_not_found"}}), AgentLimits(), Events())
    call = ToolCall("c1", "readfile", {"path": "missing.py"})

    assert executor.execute(call).result["ok"] is False          # 第一次
    assert executor.execute(ToolCall("c2", "readfile", {"path": "missing.py"})).result["ok"] is False   # 允许重试一次
    third = executor.execute(ToolCall("c3", "readfile", {"path": "missing.py"})).result
    assert third["error"]["type"] == "repeated_tool_call"
    assert third["error"]["reason"] == "identical_failure"


def test_a_successful_write_clears_repeat_memory():
    events = Events()
    registry = readonly_registry({"ok": True})
    registry.register("patch_file", lambda arguments: {"ok": True}, {"type": "object", "additionalProperties": True})
    executor = ToolExecutor(registry, AgentLimits(), events)
    call = ToolCall("c1", "readfile", {"path": "a.py"})

    assert executor.execute(call).result["ok"] is True
    assert executor.execute(ToolCall("c2", "patch_file", {"path": "a.py"})).result["ok"] is True
    # 文件被改了，同一个 readfile 应该重新放行
    assert executor.execute(ToolCall("c3", "readfile", {"path": "a.py"})).result["ok"] is True


def test_forget_signatures_allows_reading_again_after_compaction():
    executor = ToolExecutor(readonly_registry({"ok": True}), AgentLimits(), Events())
    call = ToolCall("c1", "readfile", {"path": "a.py"})
    assert executor.execute(call).result["ok"] is True
    assert executor.execute(ToolCall("c2", "readfile", {"path": "a.py"})).result["error"]["type"] == "repeated_tool_call"

    executor.forget_signatures()

    assert executor.execute(ToolCall("c3", "readfile", {"path": "a.py"})).result["ok"] is True


def test_repeat_detection_does_not_apply_to_state_changing_tools():
    registry = ToolRegistry()
    registry.register("shell", lambda arguments: {"ok": True}, {"type": "object", "additionalProperties": True})
    executor = ToolExecutor(registry, AgentLimits(), Events())
    call = ToolCall("c1", "shell", {"program": "pytest", "args": ["tests"]})

    assert executor.execute(call).result["ok"] is True
    assert executor.execute(ToolCall("c2", "shell", {"program": "pytest", "args": ["tests"]})).result["ok"] is True
