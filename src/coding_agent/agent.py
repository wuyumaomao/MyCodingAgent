from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .context import build_repository_context
from .llm import InvalidToolArguments
from .models import AssistantTurn, ToolCall
from .repository import Workspace
from .tools.registry import ToolRegistry
from .trace import RunRecorder


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable turn."""


class AgentLoop:
    def __init__(
        self,
        llm_client: Any,
        registry: ToolRegistry,
        context_builder: Callable[[Workspace], str] = build_repository_context,
        *,
        recorder: RunRecorder | None = None,
        max_rounds: int = 4,
    ) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        self.llm_client = llm_client
        self.registry = registry
        self.context_builder = context_builder
        self.recorder = recorder
        self.max_rounds = max_rounds

    def run(self, query: str, workspace: Workspace) -> str:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a read-only coding agent. Use only the provided tools, "
                    "stay inside the repository workspace, and explain findings based on evidence."
                ),
            },
            {"role": "system", "content": self.context_builder(workspace)},
            {"role": "user", "content": query},
        ]
        for round_number in range(1, self.max_rounds + 1):
            if self.recorder:
                self.recorder.record("llm_request", round=round_number, message_count=len(messages))
            try:
                turn: AssistantTurn = self.llm_client.complete(messages, self.registry.definitions())
            except InvalidToolArguments as exc:
                if self.recorder:
                    self.recorder.fail("invalid_tool_arguments", "The model returned invalid tool arguments")
                raise AgentError("The model returned invalid tool arguments") from exc
            except Exception as exc:
                if self.recorder:
                    self.recorder.fail("provider_error", "The model request failed")
                raise AgentError("The model request failed") from exc
            if self.recorder:
                self.recorder.record(
                    "llm_response",
                    round=round_number,
                    has_content=bool(turn.content),
                    tool_call_count=len(turn.tool_calls),
                )
            if not turn.tool_calls:
                answer = turn.content or "The model returned an empty response."
                if self.recorder:
                    self.recorder.complete(answer)
                return answer
            messages.append(_assistant_tool_message(turn))
            for call in turn.tool_calls:
                if self.recorder:
                    self.recorder.record(
                        "tool_call",
                        id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                    )
                result = self._execute(call)
                if self.recorder:
                    self.recorder.record(
                        "tool_result",
                        id=call.id,
                        name=call.name,
                        result=result,
                    )
                messages.append(_tool_result_message(call, result))
        answer = f"Reached the tool-call limit of {self.max_rounds} rounds before the task was complete."
        if self.recorder:
            self.recorder.fail("round_limit", "Reached the tool-call limit")
        return answer

    def _execute(self, call: ToolCall) -> dict[str, Any]:
        try:
            return self.registry.execute(call.name, call.arguments)
        except KeyError:
            return {"ok": False, "error": {"type": "unknown_tool", "message": "Unknown tool"}}
        except Exception:
            return {"ok": False, "error": {"type": "tool_error", "message": "Tool execution failed"}}


class AgentService:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    def run(self, query: str, workspace: Workspace) -> str:
        return self.loop.run(query, workspace)


def _assistant_tool_message(turn: AssistantTurn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ],
    }


def _tool_result_message(call: ToolCall, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps(result, ensure_ascii=False),
    }
