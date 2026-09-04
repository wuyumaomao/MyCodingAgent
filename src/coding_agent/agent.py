from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

from .context import ConversationContext, build_repository_context
from .llm import InvalidToolArguments, LLMResponseError, LLMTimeoutError
from .models import AssistantTurn, ToolCall
from .repository import Workspace
from .tools.registry import ToolRegistry
from .trace import RunRecorder


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable turn."""


@dataclass(frozen=True)
class AgentLimits:
    max_rounds: int = 10
    max_calls_per_tool: int = 3

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        if self.max_calls_per_tool <= 0:
            raise ValueError("max_calls_per_tool must be positive")


class AgentLoop:
    def __init__(
        self,
        llm_client: Any,
        registry: ToolRegistry,
        repository_context_builder: Callable[[Workspace], str] = build_repository_context,
        *,
        recorder: RunRecorder | None = None,
        max_rounds: int | None = None,
        limits: AgentLimits | None = None,
    ) -> None:
        if limits is not None and max_rounds is not None:
            raise ValueError("Pass limits or max_rounds, not both")
        self.limits = limits or AgentLimits(max_rounds=max_rounds or 10)
        self.llm_client = llm_client
        self.registry = registry
        self.repository_context_builder = repository_context_builder
        self.recorder = recorder
        self.max_rounds = self.limits.max_rounds
        self._tool_call_counts: dict[str, int] = {}

    def run(self, query: str, workspace: Workspace) -> str:
        context = ConversationContext(workspace, self.repository_context_builder)
        context.add_user_request(query)
        for round_number in range(1, self.max_rounds + 1):
            messages = context.messages()#获取要发送的信息
            tool_definitions = self.registry.definitions()
            if self.recorder:#record记录llm request
                self.recorder.record(
                    "llm_request",
                    round=round_number,
                    message_count=len(messages),
                    report_payload={"messages": messages, "tools": tool_definitions},
                )
            request_started = time.perf_counter()
            try:#llm返回结果
                turn: AssistantTurn = self.llm_client.complete(messages, tool_definitions)#在这里检查工具参数是否合法
            except LLMTimeoutError as exc:
                if self.recorder:
                    self.recorder.fail(
                        "timeout",
                        "The model request timed out",
                        duration_ms=_duration_ms(request_started),
                    )
                raise AgentError("The model request timed out") from exc
            except InvalidToolArguments as exc:
                if self.recorder:
                    self.recorder.fail(
                        "invalid_tool_arguments",
                        "The model returned invalid tool arguments",
                        duration_ms=_duration_ms(request_started),
                    )
                raise AgentError("The model returned invalid tool arguments") from exc
            except LLMResponseError as exc:
                if self.recorder:
                    self.recorder.fail(
                        "invalid_response",
                        "The model returned an invalid response",
                        duration_ms=_duration_ms(request_started),
                    )
                raise AgentError("The model returned an invalid response") from exc
            except Exception as exc:
                if self.recorder:
                    self.recorder.fail(
                        "provider_error",
                        "The model request failed",
                        duration_ms=_duration_ms(request_started),
                    )
                raise AgentError("The model request failed") from exc
            if self.recorder:#record记录llm response
                response_payload: dict[str, object] = {
                    "round": round_number,
                    "has_content": bool(turn.content),
                    "tool_call_count": len(turn.tool_calls),
                    "duration_ms": _duration_ms(request_started),
                }
                self.recorder.record(
                    "llm_response",
                    report_payload={
                        "content": turn.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in turn.tool_calls
                        ],
                    },
                    **response_payload,
                )
            if not turn.tool_calls:#若非toolcall则视为结束
                answer = turn.content or "The model returned an empty response."
                if self.recorder:
                    self.recorder.complete(answer)
                return answer
            context.add_assistant_turn(turn)#工具调用来了，追加 assistant 消息
            for call in turn.tool_calls:#把工具调用记录写到trace
                if self.recorder:
                    arguments_summary = _tool_arguments_summary(call.name, call.arguments)
                    self.recorder.record(
                        "tool_call",
                        id=call.id,
                        name=call.name,
                        arguments=arguments_summary,
                        report_payload={"arguments": call.arguments},
                    )
                current_count = self._tool_call_counts.get(call.name, 0)
                if current_count >= self.limits.max_calls_per_tool:
                    result = {
                        "ok": False,
                        "error": {
                            "type": "tool_call_limit",
                            "message": f"Tool {call.name} reached its call limit",
                        },
                    }
                    if self.recorder:
                        self.recorder.record(
                            "tool_call_limit",
                            name=call.name,
                            max_calls=self.limits.max_calls_per_tool,
                            observed=current_count,
                        )
                    tool_duration = 0.0
                else:
                    self._tool_call_counts[call.name] = current_count + 1
                    tool_started = time.perf_counter()
                    result = self._execute(call)#执行call
                    tool_duration = _duration_ms(tool_started)
                if self.recorder:#记录工具调用的结果
                    self.recorder.record(
                        "tool_result",
                        id=call.id,
                        name=call.name,
                        ok=result.get("ok"),
                        duration_ms=tool_duration,
                        report_payload={"result": result},
                    )
                context.add_tool_result(call, result)#追加 tool 结果消息
        answer = f"Reached the tool-call limit of {self.max_rounds} rounds before the task was complete."
        if self.recorder:#执行到循环外部了，则触发结束
            self.recorder.fail("round_limit", "Reached the tool-call limit")
        return answer

    def _execute(self, call: ToolCall) -> dict[str, Any]:
        try:
            return self.registry.execute(call.name, call.arguments)
        except KeyError:
            return {"ok": False, "error": {"type": "unknown_tool", "message": "Unknown tool"}}
        except Exception:
            return {"ok": False, "error": {"type": "tool_error", "message": "Tool execution failed"}}


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _tool_arguments_summary(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep large write payloads out of the concise trace while preserving report detail."""
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


class AgentService:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    def run(self, query: str, workspace: Workspace) -> str:
        return self.loop.run(query, workspace)
