from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

from .context import ConversationContext, build_repository_context
from .model_gateway import ModelGateway, ModelGatewayError
from .models import AssistantTurn, ToolCall
from .repository import Workspace
from .tools.registry import ToolRegistry
from .trace import RunRecorder
from .events import NullEventSink, RecorderEventSink
from .tool_executor import ToolExecutor


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
        self.model_gateway = ModelGateway(llm_client)
        self.registry = registry
        self.repository_context_builder = repository_context_builder
        self.recorder = recorder
        self.max_rounds = self.limits.max_rounds

    def run(self, query: str, workspace: Workspace, *, event_sink: Any | None = None) -> str:
        context = ConversationContext(workspace, self.repository_context_builder)
        context.add_user_request(query)
        sink = event_sink or (RecorderEventSink(self.recorder) if self.recorder else NullEventSink())
        tool_executor = ToolExecutor(self.registry, self.limits, sink)
        for round_number in range(1, self.max_rounds + 1):
            messages = context.messages()#获取要发送的信息
            tool_definitions = self.registry.definitions()
            sink.emit(
                "llm_request",
                round=round_number,
                message_count=len(messages),
                report_payload={"messages": messages, "tools": tool_definitions},
            )
            request_started = time.perf_counter()
            try:#llm返回结果
                turn: AssistantTurn = self.model_gateway.complete(messages, tool_definitions)
            except ModelGatewayError as exc:
                sink.fail(
                    exc.error_type,
                    exc.public_message,
                    duration_ms=_duration_ms(request_started),
                )
                raise AgentError(exc.public_message) from exc
            response_payload: dict[str, object] = {
                "round": round_number,
                "has_content": bool(turn.content),
                "tool_call_count": len(turn.tool_calls),
                "duration_ms": _duration_ms(request_started),
            }
            sink.emit(
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
                sink.complete(answer)
                return answer
            context.add_assistant_turn(turn)#工具调用来了，追加 assistant 消息
            for call in turn.tool_calls:#把工具调用记录写到trace
                execution = tool_executor.execute(call)
                context.add_tool_result(call, execution.result)#追加 tool 结果消息
        answer = f"Reached the tool-call limit of {self.max_rounds} rounds before the task was complete."
        sink.fail("round_limit", "Reached the tool-call limit")
        return answer

def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


class AgentService:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    def run(self, query: str, workspace: Workspace) -> str:
        return self.loop.run(query, workspace)
