from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any

from .events import EventSink
from .models import ToolCall
from .tools.registry import ToolRegistry
from .tools.schema import ToolSchemaError
from .tools.approval import event_sink_context


# 只读工具：相同参数再次调用返回相同结果（除非期间发生写操作或上下文压缩），
# 所以重复调用可以安全拒绝。shell 与写工具不参与——它们的结果依赖外部状态，
# 同一个命令跑两次可能是合理的。
_READ_ONLY_TOOLS = frozenset({"listfiles", "readfile", "search", "find_files"})
# 成功执行后工作区或外部状态可能改变，必须清空重复检测记忆。
_STATE_CHANGING_TOOLS = frozenset({"write_file", "patch_file", "shell"})
# 同一个调用失败多少次之后不再重试
_MAX_IDENTICAL_FAILURES = 2


@dataclass(frozen=True)
class ToolExecution:
    result: dict[str, Any]
    duration_ms: float


class ToolExecutor:
    """Execute tool calls, rejecting repeats instead of counting calls per tool.

    按工具计数的硬上限会拦掉合法用法（读一个 574 行的文件要分 5~8 次区间读取），
    却拦不住真正有害的重复调用。因此这里只拒绝「完全相同的调用」：成功过的调用
    再次出现说明结果已在上下文中，失败两次的调用再试也不会变。其余一律放行。
    """

    def __init__(self, registry: ToolRegistry, limits: Any, event_sink: EventSink) -> None:
        self.registry = registry
        self.limits = limits
        self.event_sink = event_sink
        self._tool_call_counts: dict[str, int] = {}
        self._succeeded: set[str] = set()
        self._failed: dict[str, int] = {}

    def forget_signatures(self) -> None:
        """Drop repeat memory because earlier results may no longer be visible."""
        self._succeeded.clear()
        self._failed.clear()

    def execute(self, call: ToolCall) -> ToolExecution:
        self.event_sink.emit(
            "tool_call",
            id=call.id,
            name=call.name,
            arguments=_tool_arguments_summary(call.name, call.arguments),
            report_payload={"arguments": call.arguments},
        )
        signature = _signature(call)
        repeated = self._repeat_error(call, signature)
        if repeated is not None:
            result = repeated
            self.event_sink.emit(
                "tool_call_repeat",
                name=call.name,
                reason=repeated["error"]["reason"],
                failures=self._failed.get(signature, 0),
            )
            execution = ToolExecution(result, 0.0)
        else:
            current_count = self._tool_call_counts.get(call.name, 0)
            if current_count >= self.limits.max_calls_per_tool:
                # 仅作为安全阀：默认值放宽到正常任务远达不到的程度。
                result = {
                    "ok": False,
                    "error": {"type": "tool_call_limit", "message": f"Tool {call.name} reached its safety valve of {self.limits.max_calls_per_tool} calls; use another tool"},
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
                self._record(call, signature, result)
        self.event_sink.emit(
            "tool_result",
            id=call.id,
            name=call.name,
            ok=execution.result.get("ok"),
            error_type=(execution.result.get("error") or {}).get("type")
            if isinstance(execution.result.get("error"), dict)
            else None,
            duration_ms=execution.duration_ms,
            report_payload={"result": execution.result},
        )
        return execution

    def _repeat_error(self, call: ToolCall, signature: str) -> dict[str, Any] | None:
        if call.name not in _READ_ONLY_TOOLS:
            return None
        if self._failed.get(signature, 0) >= _MAX_IDENTICAL_FAILURES:
            return {
                "ok": False,
                "error": {
                    "type": "repeated_tool_call",
                    "reason": "identical_failure",
                    "message": f"Identical {call.name} call already failed {_MAX_IDENTICAL_FAILURES} times; change the arguments or use another tool",
                },
            }
        if signature in self._succeeded:
            return {
                "ok": False,
                "error": {
                    "type": "repeated_tool_call",
                    "reason": "already_succeeded",
                    "message": f"Identical {call.name} call already succeeded and its result is already in the conversation; reuse it or change the arguments (for example a narrower start/end range)",
                },
            }
        return None

    def _record(self, call: ToolCall, signature: str, result: dict[str, Any]) -> None:
        if call.name in _STATE_CHANGING_TOOLS and result.get("ok") is True:
            self.forget_signatures()
            return
        if call.name not in _READ_ONLY_TOOLS:
            return
        if result.get("ok") is True:
            self._succeeded.add(signature)
        else:
            self._failed[signature] = self._failed.get(signature, 0) + 1


def _signature(call: ToolCall) -> str:
    return f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"


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
