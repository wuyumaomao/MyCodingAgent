from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..models import AssistantTurn, ToolCall


class ScriptedLLM:
    """Deterministic model double used to test the complete benchmark harness."""

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self.turns = tuple(turns)
        self.index = 0
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        if self.index >= len(self.turns):
            raise AssertionError("Scripted LLM turns exhausted")
        turn = self.turns[self.index]
        self.index += 1
        copied_messages = deepcopy(messages)
        copied_tools = deepcopy(tools)
        self.requests.append((copied_messages, copied_tools))
        available = {item.get("function", {}).get("name") for item in tools if isinstance(item, dict)}
        for call in turn.tool_calls:
            if call.name not in available:
                raise AssertionError(f"Tool {call.name!r} is not registered in the request")
        return replace(turn, tool_calls=list(turn.tool_calls))


def turns_for_task(task_id: str) -> tuple[AssistantTurn, ...]:
    if task_id == "readme_patch_basic":
        return (
            AssistantTurn(None, [ToolCall("read-1", "readfile", {"path": "README.md"})]),
            AssistantTurn(None, [ToolCall("patch-1", "patch_file", {"path": "README.md", "old_text": "PLACEHOLDER_INTRO", "new_text": "This repository is evaluated by the coding agent benchmark."})]),
            AssistantTurn("README updated successfully.", []),
        )
    if task_id == "write_file_create":
        return (
            AssistantTurn(None, [ToolCall("read-1", "readfile", {"path": "README.md"})]),
            AssistantTurn(None, [ToolCall("write-1", "write_file", {"path": "scripts/hello.py", "content": "print('hello benchmark')\n"})]),
            AssistantTurn("The script was created.", []),
        )
    if task_id == "invalid_arguments_recovery":
        return (
            AssistantTurn(None, [ToolCall("bad-1", "readfile", {"path": 123})]),
            AssistantTurn(None, [ToolCall("read-1", "readfile", {"path": "README.md"})]),
            AssistantTurn("This repository is a coding agent benchmark fixture.", []),
        )
    if task_id == "path_escape_recovery":
        return (
            AssistantTurn(None, [ToolCall("bad-1", "readfile", {"path": "../README.md"})]),
            AssistantTurn(None, [ToolCall("read-1", "readfile", {"path": "README.md"})]),
            AssistantTurn("This repository is a coding agent benchmark fixture.", []),
        )
    raise KeyError(f"No Fake LLM script for task {task_id!r}")
