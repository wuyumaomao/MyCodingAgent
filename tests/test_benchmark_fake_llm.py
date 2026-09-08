from __future__ import annotations

import pytest

from coding_agent.benchmark.fake_llm import ScriptedLLM, turns_for_task
from coding_agent.models import AssistantTurn, ToolCall


def test_scripted_llm_returns_turns_in_order_and_records_requests():
    llm = ScriptedLLM([AssistantTurn("done", [])])
    messages = [{"role": "user", "content": "hi"}]
    result = llm.complete(messages, [])
    assert result.content == "done"
    assert llm.requests == [(messages, [])]


def test_scripted_llm_rejects_exhaustion_and_missing_tool_definition():
    llm = ScriptedLLM([AssistantTurn(None, [ToolCall("1", "readfile", {"path": "README.md"})])])
    with pytest.raises(AssertionError, match="not registered"):
        llm.complete([], [])
    llm = ScriptedLLM([AssistantTurn("done", [])])
    llm.complete([], [])
    with pytest.raises(AssertionError, match="exhausted"):
        llm.complete([], [])


@pytest.mark.parametrize("task_id", [
    "readme_patch_basic", "write_file_create", "invalid_arguments_recovery", "path_escape_recovery"
])
def test_fake_sequences_exist_for_all_tasks(task_id):
    turns = turns_for_task(task_id)
    assert turns
    assert isinstance(turns[-1], AssistantTurn)
