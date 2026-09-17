from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
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
from .memory import MemoryManager
from .session import SessionState, SessionStore


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable turn."""


@dataclass(frozen=True)
class AgentLimits:
    max_rounds: int = 20
    max_calls_per_tool: int = 3
    max_llm_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        if self.max_calls_per_tool <= 0:
            raise ValueError("max_calls_per_tool must be positive")
        if self.max_llm_retries < 0:
            raise ValueError("max_llm_retries must be non-negative")


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
        session: SessionState | None = None,
        session_store: SessionStore | None = None,
        summary_provider: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
        result_summary_provider: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if limits is not None and max_rounds is not None:
            raise ValueError("Pass limits or max_rounds, not both")
        self.limits = limits or AgentLimits(max_rounds=max_rounds or 20)
        self.llm_client = llm_client
        self.model_gateway = ModelGateway(llm_client, max_retries=self.limits.max_llm_retries)
        self.registry = registry
        self.repository_context_builder = repository_context_builder
        self.recorder = recorder
        self.max_rounds = self.limits.max_rounds
        self.session = session
        self.session_store = session_store
        self.summary_provider = summary_provider
        self.result_summary_provider = result_summary_provider

    def run(self, query: str, workspace: Workspace, *, event_sink: Any | None = None) -> str:
        #构造prompt前缀
        memory = MemoryManager(self.session, summary_provider=self.summary_provider) if self.session is not None else None
        context = ConversationContext(
            workspace,
            self.repository_context_builder,
            memory=memory,
            result_summary_provider=self.result_summary_provider,
        )
        if self.session is not None:
            context.history = self.session.history
            memory.begin_run(query)
        context.add_user_request(query)
        sink = event_sink or (RecorderEventSink(self.recorder) if self.recorder else NullEventSink())
        tool_executor = ToolExecutor(self.registry, self.limits, sink)
        for round_number in range(1, self.max_rounds + 1):
            messages = context.messages(query if memory is not None else None)#获取要发送的信息
            tool_definitions = self.registry.definitions()
            # Measure the prompt already built for this round; rebuilding it would
            # re-run the tool result summarizer.
            metrics = context.prompt_metrics(messages) if memory is not None else {}
            metrics.pop("prompt_chars", None)
            sink.emit(
                "llm_request",
                round=round_number,
                message_count=len(messages),
                prompt_chars=_prompt_chars(messages, tool_definitions),
                report_payload={"messages": messages, "tools": tool_definitions},
                **({"session_id": self.session.session_id, **metrics} if self.session is not None else {}),
            )
            request_started = time.perf_counter()
            retry_count = 0

            def record_retry(payload: dict[str, object]) -> None:
                nonlocal retry_count
                retry_count += 1
                sink.emit("llm_retry", round=round_number, **payload)

            try:#llm返回结果
                turn: AssistantTurn = self.model_gateway.complete(
                    messages, tool_definitions, on_retry=record_retry
                )
            except ModelGatewayError as exc:
                sink.fail(
                    exc.error_type,
                    exc.public_message,
                    duration_ms=_duration_ms(request_started),
                )
                if self.session is not None and self.session_store is not None:
                    self.session_store.save(self.session)
                raise AgentError(exc.public_message) from exc
            response_payload: dict[str, object] = {
                "round": round_number,
                "has_content": bool(turn.content),
                "tool_call_count": len(turn.tool_calls),
                "duration_ms": _duration_ms(request_started),
                "finish_reason": getattr(turn, "finish_reason", None),
                "finish_reason_source": getattr(turn, "finish_reason_source", None),
                "api_attempts": retry_count + 1,
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
                if self.session is not None:
                    context.history.append({"role": "assistant", "content": answer})
                    if self.session_store is not None:
                        self.session_store.save(self.session)
                sink.complete(answer)
                return answer
            context.add_assistant_turn(turn)#工具调用来了，追加 assistant 消息
            for call in turn.tool_calls:#把工具调用记录写到trace
                execution = tool_executor.execute(call)
                if memory is not None:
                    raw_content = None
                    if call.name == "readfile" and execution.result.get("ok") is True:
                        path = execution.result.get("path")
                        start, end, count = execution.result.get("start"), execution.result.get("end"), execution.result.get("line_count")
                        if isinstance(path, str) and start == 1 and end == count:
                            try:
                                raw_content = workspace.resolve_relative(path).read_text(encoding="utf-8")
                            except (OSError, UnicodeError):
                                raw_content = None
                    memory.observe_tool_result(call, execution.result, run_id=self.session.session_id, round_number=round_number, raw_content=raw_content)
                context.add_tool_result(call, execution.result)#追加 tool 结果消息
                if self.session_store is not None:
                    self.session_store.save(self.session)
        if self.session_store is not None and self.session is not None:
            self.session_store.save(self.session)
        answer = f"Reached the round limit of {self.max_rounds} before the task was complete."
        sink.fail("round_limit", "Reached the round limit")
        return answer

def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _prompt_chars(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """Count the serialized request context sent to the model."""
    payload = {"messages": messages, "tools": tools}
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class AgentService:
    """Compatibility adapter for callers that still use AgentService."""

    def __init__(self, loop: AgentLoop | Any, *, recorder: RunRecorder | None = None) -> None:
        self.loop = loop
        self.recorder = recorder

    def run(self, query: str, workspace: Workspace) -> str:
        if hasattr(self.loop, "ask"):
            return self.loop.ask(query, recorder=self.recorder)
        return self.loop.run(query, workspace)
