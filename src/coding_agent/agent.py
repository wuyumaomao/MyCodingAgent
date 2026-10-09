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
from .session import (
    SessionState,
    SessionStore,
    begin_checkpoint,
    cancel_checkpoint,
    finish_checkpoint,
    mark_checkpoint_result,
    mark_checkpoint_running,
    reconcile_checkpoint,
    runtime_identity_mismatches,
    _now,
)


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable turn."""


@dataclass(frozen=True)
class AgentLimits:
    max_rounds: int = 20
    # 安全阀而非工作预算：重复检测才是主要机制（见 ToolExecutor）。
    # 读一个几百行的文件需要多次区间读取，默认值必须远高于此。
    max_calls_per_tool: int = 30
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
        compaction_summarizer: Callable[[list[dict[str, Any]]], str] | None = None,
        transcript_budget_chars: int | None = None,
        context_window_tokens: int | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        if limits is not None and max_rounds is not None:
            raise ValueError("Pass limits or max_rounds, not both")
        self.transcript_budget_chars = transcript_budget_chars
        self.context_window_tokens = context_window_tokens
        self.runtime_identity = dict(runtime_identity or {})
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
        self.compaction_summarizer = compaction_summarizer

    def run(self, query: str, workspace: Workspace, *, event_sink: Any | None = None) -> str:
        try:
            return self._run(query, workspace, event_sink=event_sink)
        except KeyboardInterrupt:
            self._mark_run_cancelled(reason="keyboard_interrupt")
            raise

    def _run(self, query: str, workspace: Workspace, *, event_sink: Any | None = None) -> str:
        sink = event_sink or (RecorderEventSink(self.recorder) if self.recorder else NullEventSink())
        #构造prompt前缀
        memory = MemoryManager(self.session, summary_provider=self.summary_provider, event_sink=sink) if self.session is not None else None
        context = ConversationContext(
            workspace,
            self.repository_context_builder,
            memory=memory,
            compaction_summarizer=self.compaction_summarizer,
            event_sink=sink,
            **({"transcript_budget_chars": self.transcript_budget_chars} if self.transcript_budget_chars is not None else {}),
            **({"context_window_tokens": self.context_window_tokens} if self.context_window_tokens is not None else {}),
        )
        history_start = len(self.session.history) if self.session is not None else None
        if self.session is not None:
            context.history = self.session.history
            context.history_exclusions = self.session.cancelled_runs
            memory.begin_run(query)
            self._resume_active_checkpoint(context, memory, workspace)
        context.add_user_request(query)
        run_id = self.recorder.run_id if self.recorder is not None else f"session-{int(time.time() * 1000)}"
        if self.session is not None:
            self.session.run_state = {
                "status": "running",
                "run_id": run_id,
                "query": query,
                "round": 0,
                "reason": None,
                "started_at": _now(),
                "ended_at": None,
                "history_start": history_start,
                "history_end": None,
            }
            if self.session_store is not None:
                self.session_store.save(self.session)
        tool_executor = ToolExecutor(self.registry, self.limits, sink)
        seen_compaction = 0
        for round_number in range(1, self.max_rounds + 1):
            if self.session is not None:
                self.session.run_state["round"] = round_number
            messages = context.messages(query if memory is not None else None)#获取要发送的信息
            if context.compaction_serial != seen_compaction:
                # 压缩把旧工具结果换成了摘要，被压掉的结果可以重新读取。
                seen_compaction = context.compaction_serial
                tool_executor.forget_signatures()
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
                if self.session is not None:
                    self.session.run_state.update({"status": "failed", "reason": exc.error_type, "ended_at": _now()})
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
                if self.session is not None:
                    self.session.run_state.update({"status": "completed", "ended_at": _now()})
                    if self.session_store is not None:
                        self.session_store.save(self.session)
                return answer
            if self.session is not None and self.session_store is not None:
                begin_checkpoint(self.session, run_id, round_number, turn.tool_calls, workspace)
            context.add_assistant_turn(turn)#工具调用来了，追加 assistant 消息
            if self.session_store is not None and self.session is not None:
                self.session_store.save(self.session)
            for call in turn.tool_calls:#把工具调用记录写到trace
                if self.session is not None and self.session_store is not None:
                    mark_checkpoint_running(self.session, call.id, workspace, call.arguments)
                    self.session_store.save(self.session)
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
                    if execution.result.get("ok") is True and call.name in {"write_file", "patch_file"}:
                        context.invalidate_compaction()
                context.add_tool_result(call, execution.result)#追加 tool 结果消息
                if self.session is not None and self.session_store is not None:
                    mark_checkpoint_result(self.session, call.id, execution.result)
                if self.session_store is not None:
                    self.session_store.save(self.session)
            if self.session is not None and self.session_store is not None:
                finish_checkpoint(self.session)
                self.session_store.save(self.session)
        if self.session_store is not None and self.session is not None:
            self.session_store.save(self.session)
        answer = f"Reached the round limit of {self.max_rounds} before the task was complete."
        sink.fail("round_limit", "Reached the round limit")
        if self.session is not None:
            self.session.run_state.update({"status": "failed", "reason": "round_limit", "ended_at": _now()})
            if self.session_store is not None:
                self.session_store.save(self.session)
        return answer

    def _mark_run_cancelled(self, *, reason: str) -> None:
        if self.session is None:
            return
        self.session.run_state.update({"status": "cancelled", "reason": reason, "ended_at": _now()})
        start = self.session.run_state.get("history_start")
        if isinstance(start, int):
            if not isinstance(self.session.cancelled_runs, list):
                self.session.cancelled_runs = []
            record = {"run_id": self.session.run_state.get("run_id"), "start": start, "end": len(self.session.history)}
            self.session.cancelled_runs.append(record)
            self.session.run_state["history_end"] = record["end"]
        active = self.session.checkpoints.get("active")
        if not isinstance(active, dict) and self.session.history and self.session.history[-1].get("role") == "user":
            self.session.history.append({"role": "assistant", "content": "Task cancelled by user."})
        if isinstance(start, int) and self.session.cancelled_runs:
            self.session.cancelled_runs[-1]["end"] = len(self.session.history)
            self.session.run_state["history_end"] = len(self.session.history)
        if self.session_store is not None:
            self.session_store.save(self.session)

    def _resume_active_checkpoint(self, context: ConversationContext, memory: MemoryManager, workspace: Workspace) -> None:
        if self.session is None or self.session_store is None:
            return
        active = self.session.checkpoints.get("active")
        if not isinstance(active, dict):
            return
        if self.session.run_state.get("status") == "cancelled" and self.session.run_state.get("run_id") == active.get("run_id"):
            self._recover_cancelled_checkpoint(context, memory, active)
            return
        mismatches = runtime_identity_mismatches(self.session, self.runtime_identity)
        if mismatches:
            unresolved = []
            tool_results = []
            for item in active.get("calls") or []:
                if not isinstance(item, dict):
                    continue
                call_id = str(item.get("id", ""))
                name = str(item.get("name", ""))
                arguments = dict(item.get("arguments") or {})
                unresolved.append({"id": call_id, "tool": name, "state": "runtime_mismatch"})
                tool_results.append({
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "result": {
                        "ok": False,
                        "error": {
                            "type": "execution_interrupted",
                            "message": "Recovery was not attempted because runtime identity changed: " + ", ".join(mismatches),
                        },
                    },
                })
            reconciliation = {
                "tool_results": tool_results,
                "resume_state": {
                    "status": "runtime_mismatch",
                    "checkpoint_id": active.get("id"),
                    "checked_at": _now(),
                    "workspace_changed": False,
                    "unresolved_calls": unresolved,
                    "runtime_mismatches": mismatches,
                },
            }
        else:
            reconciliation = reconcile_checkpoint(workspace, active)
        known_ids = {
            str(call.get("id"))
            for message in context.history
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        }
        known_result_ids = {
            str(message.get("tool_call_id"))
            for message in context.history
            if message.get("role") == "tool" and message.get("tool_call_id") is not None
        }
        calls = []
        for item in active.get("calls") or []:
            if isinstance(item, dict) and str(item.get("id")) not in known_ids:
                calls.append(ToolCall(str(item.get("id")), str(item.get("name")), dict(item.get("arguments") or {})))
        if calls:
            context.add_assistant_turn(AssistantTurn(None, calls))
        for item in reconciliation["tool_results"]:
            call = ToolCall(str(item["id"]), str(item["name"]), dict(item.get("arguments") or {}))
            if call.id in known_result_ids:
                continue
            context.add_tool_result(call, item["result"])
            memory.observe_tool_result(call, item["result"], run_id=self.session.session_id, round_number=int(active.get("round", 0) or 0))
        self.session.resume_state = reconciliation["resume_state"]
        finish_checkpoint(self.session)
        self.session.resume_state = reconciliation["resume_state"]
        self.session_store.save(self.session)

    def _recover_cancelled_checkpoint(self, context: ConversationContext, memory: MemoryManager, active: dict[str, Any]) -> None:
        known_ids = {
            str(call.get("id"))
            for message in context.history
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        }
        known_result_ids = {
            str(message.get("tool_call_id"))
            for message in context.history
            if message.get("role") == "tool" and message.get("tool_call_id") is not None
        }
        calls = [
            ToolCall(str(item.get("id")), str(item.get("name")), dict(item.get("arguments") or {}))
            for item in active.get("calls") or []
            if isinstance(item, dict) and str(item.get("id")) not in known_ids
        ]
        if calls:
            context.add_assistant_turn(AssistantTurn(None, calls))
        unresolved: list[dict[str, Any]] = []
        for item in active.get("calls") or []:
            if not isinstance(item, dict):
                continue
            call_id = str(item.get("id"))
            if call_id in known_result_ids:
                continue
            was_running = item.get("status") in {"running", "interrupted"}
            if was_running:
                unresolved.append({"id": call_id, "tool": str(item.get("name", "")), "state": "unknown"})
            result = {
                "ok": False,
                "error": {
                    "type": "execution_interrupted" if was_running else "execution_cancelled",
                    "message": "Tool execution was interrupted by user cancellation" if was_running else "Tool call was cancelled by user",
                },
            }
            call = ToolCall(call_id, str(item.get("name", "")), dict(item.get("arguments") or {}))
            context.add_tool_result(call, result)
            memory.observe_tool_result(call, result, run_id=self.session.session_id, round_number=int(active.get("round", 0) or 0))
        cancel_checkpoint(self.session, reason=str(self.session.run_state.get("reason") or "keyboard_interrupt"))
        resume_state = {
            "status": "cancelled",
            "checkpoint_id": active.get("id"),
            "checked_at": _now(),
            "workspace_changed": False,
            "unresolved_calls": unresolved,
        }
        finish_checkpoint(self.session, status="cancelled")
        for item in reversed(self.session.cancelled_runs):
            if isinstance(item, dict) and item.get("run_id") == active.get("run_id"):
                item["end"] = len(self.session.history)
                self.session.run_state["history_end"] = item["end"]
                break
        self.session.resume_state = resume_state
        self.session_store.save(self.session)

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
