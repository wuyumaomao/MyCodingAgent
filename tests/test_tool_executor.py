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
