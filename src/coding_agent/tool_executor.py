from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from .events import EventSink
from .models import ToolCall
from .tools.registry import ToolRegistry
from .tools.schema import ToolSchemaError
from .tools.approval import event_sink_context


@dataclass(frozen=True)
class ToolExecution:
    result: dict[str, Any]
    duration_ms: float


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, limits: Any, event_sink: EventSink) -> None:
        self.registry = registry
        self.limits = limits
        self.event_sink = event_sink
        self._tool_call_counts: dict[str, int] = {}

    def execute(self, call: ToolCall) -> ToolExecution:
        self.event_sink.emit(
            "tool_call",
            id=call.id,
            name=call.name,
            arguments=_tool_arguments_summary(call.name, call.arguments),
            report_payload={"arguments": call.arguments},
        )
        current_count = self._tool_call_counts.get(call.name, 0)
        if current_count >= self.limits.max_calls_per_tool:
            result = {
                "ok": False,
                "error": {"type": "tool_call_limit", "message": f"Tool {call.name} reached its call limit"},
            }
            self.event_sink.emit(
                "tool_call_limit",
                name=call.name,
                max_calls=self.limits.max_calls_per_tool,
                observed=current_count,
            )
            execution = ToolExecution(result, 0.0)
        else:
            self._tool_call_counts[call.name] = current_count + 1
            started = time.perf_counter()
            with event_sink_context(self.event_sink):
                try:  # 在这里完成 schema 检测
                    self.registry.validate(call.name, call.arguments)  # schema 验证
                    result = self.registry.execute(call.name, call.arguments)
                except KeyError:
                    result = {"ok": False, "error": {"type": "unknown_tool", "message": "Unknown tool"}}
                except ToolSchemaError:
                    result = {"ok": False, "error": {"type": "invalid_tool_arguments", "message": "Tool arguments do not match the registered schema"}}
                except Exception:
                    result = {"ok": False, "error": {"type": "tool_error", "message": "Tool execution failed"}}
            execution = ToolExecution(result, _duration_ms(started))
        self.event_sink.emit(
            "tool_result",
            id=call.id,
            name=call.name,
            ok=execution.result.get("ok"),
            duration_ms=execution.duration_ms,
            report_payload={"result": execution.result},
        )
        return execution


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _tool_arguments_summary(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "write_file":
        summary = {key: value for key, value in arguments.items() if key != "content"}
        content = arguments.get("content")
        if isinstance(content, str):
            summary["content_bytes"] = len(content.encode("utf-8"))
        return summary
    if name == "patch_file":
        summary = {key: value for key, value in arguments.items() if key not in {"old_text", "new_text"}}
        for key in ("old_text", "new_text"):
            value = arguments.get(key)
            if isinstance(value, str):
                summary[f"{key}_bytes"] = len(value.encode("utf-8"))
        return summary
    return dict(arguments)
