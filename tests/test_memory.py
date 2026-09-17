import json
from pathlib import Path

import pytest

from coding_agent.memory import (
    FILE_SUMMARY_SYSTEM_PROMPT,
    LLMFileSummaryProvider,
    LLMToolResultSummaryProvider,
    MemoryManager,
    _keywords,
)
from coding_agent.models import ToolCall
from coding_agent.session import SessionState, empty_memory


def make_memory():
    return MemoryManager(SessionState("session-1", Path("C:/repo"), memory=empty_memory()))


def test_full_read_creates_summary_and_recent_file():
    memory = make_memory()
    memory.observe_tool_result(ToolCall("1", "readfile", {"path": "a.py"}), {"ok": True, "path": "a.py", "content": "1: def main():\n2:     pass", "start": 1, "end": 2, "line_count": 2}, run_id="r", round_number=1)
    assert memory.data["working_memory"]["recent_files"] == ["a.py"]
    assert len(memory.data["file_summaries"]["a.py"]["freshness"]) == 64


def test_patch_invalidates_summary():
    memory = make_memory()
    memory.data["file_summaries"]["a.py"] = {"freshness": "x"}
    memory.observe_tool_result(ToolCall("1", "patch_file", {"path": "a.py"}), {"ok": True, "path": "a.py"}, run_id="r", round_number=1)
    assert "a.py" not in memory.data["file_summaries"]


def test_relevant_memory_is_limited_to_three():
    memory = make_memory()
    for i in range(5):
        memory.data["episodic_notes"].append({"id": str(i), "content": f"pytest note {i}", "keywords": ["pytest"], "source": {}, "created_at": str(i)})
    assert len(memory.retrieve_relevant("run pytest")) == 3


def test_file_summaries_are_retrieved_by_query_not_rendered_all():
    memory = make_memory()
    memory.data["file_summaries"] = {
        "src/httpstat.py": {"summary": "HTTP CLI entrypoint", "symbols": ["main"], "freshness": "a"},
        "tests/test_httpstat.py": {"summary": "pytest coverage", "symbols": ["test_json"], "freshness": "b"},
        "docs/old.md": {"summary": "unrelated history", "symbols": [], "freshness": "c"},
    }
    result = memory.retrieve_file_summaries("httpstat main", limit=5)
    paths = [item["path"] for item in result]
    assert "src/httpstat.py" in paths
    assert "docs/old.md" not in paths


def test_file_summaries_are_retrieved_by_symbol_name():
    """A query naming a top-level symbol must recall the file that defines it."""
    memory = make_memory()
    memory.data["file_summaries"] = {
        "src/httpstat.py": {"summary": "HTTP CLI entrypoint", "symbols": ["main"], "freshness": "a"},
        "tests/test_httpstat.py": {"summary": "pytest coverage", "symbols": ["test_json"], "freshness": "b"},
        "docs/old.md": {"summary": "unrelated history", "symbols": [], "freshness": "c"},
    }

    paths = [item["path"] for item in memory.retrieve_file_summaries("test_json", limit=5)]

    assert paths == ["tests/test_httpstat.py"]


def test_full_read_uses_injected_summary_provider():
    calls = []

    def provider(path, content, result):
        calls.append(path)
        return {"summary": "provider summary", "symbols": ["main"], "line_index": [{"lines": "1-2", "desc": "entry"}]}

    memory = MemoryManager(make_memory().session, summary_provider=provider)
    memory.observe_tool_result(ToolCall("1", "readfile", {"path": "a.py"}), {"ok": True, "path": "a.py", "content": "1: def main():\n2:     pass", "start": 1, "end": 2, "line_count": 2}, run_id="r", round_number=1)
    assert calls == ["a.py"]
    assert memory.data["file_summaries"]["a.py"]["summary"] == "provider summary"


def test_summary_provider_receives_raw_content_when_available():
    seen = []

    def provider(path, content, result):
        seen.append(content)
        return {"summary": "raw", "symbols": [], "line_index": []}

    memory = MemoryManager(make_memory().session, summary_provider=provider)
    result = {"ok": True, "path": "a.py", "content": "1: def main():", "start": 1, "end": 1, "line_count": 1}
    memory.observe_tool_result(ToolCall("1", "readfile", {"path": "a.py"}), result, run_id="r", round_number=1, raw_content="def main():")
    assert seen == ["def main():"]


def test_llm_file_summary_provider_uses_strict_json_contract():
    captured = []

    def complete_text(messages):
        captured.append(messages)
        return '{"summary":"CLI entrypoint","symbols":["main"],"line_index":[{"lines":"1-2","desc":"entry"}]}'

    summary = LLMFileSummaryProvider(complete_text)("a.py", "def main():\n    pass", {"line_count": 2})
    assert summary["summary"] == "CLI entrypoint"
    assert captured[0][0]["role"] == "system"
    assert "工具" not in captured[0][0].get("tools", {})


def test_same_file_hash_reuses_existing_summary_without_provider_call():
    calls = []

    def provider(path, content, result):
        calls.append(path)
        return {"summary": "cached", "symbols": [], "line_index": []}

    memory = MemoryManager(make_memory().session, summary_provider=provider)
    call = ToolCall("1", "readfile", {"path": "a.py"})
    result = {"ok": True, "path": "a.py", "content": "1: def main():", "start": 1, "end": 1, "line_count": 1}
    memory.observe_tool_result(call, result, run_id="r", round_number=1, raw_content="def main():")
    memory.observe_tool_result(call, result, run_id="r", round_number=2, raw_content="def main():")
    assert calls == ["a.py"]


def test_llm_provider_output_is_retrievable_by_symbol_and_rendered():
    """Lock the provider -> retrieval/render contract to one field vocabulary."""
    complete_text = lambda messages: json.dumps(
        {
            "summary": "受控 shell 工具的策略层",
            "symbols": ["ShellPolicy", "validate"],
            "line_index": [{"lines": "96-148", "desc": "各 program 的参数校验"}],
        }
    )
    memory = MemoryManager(make_memory().session, summary_provider=LLMFileSummaryProvider(complete_text))

    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "src/shell_policy.py"}),
        {"ok": True, "path": "src/shell_policy.py", "content": "1: class ShellPolicy:", "start": 1, "end": 3, "line_count": 3},
        run_id="r",
        round_number=1,
        raw_content="class ShellPolicy:\n    pass\n    pass\n",
    )

    assert [item["path"] for item in memory.retrieve_file_summaries("ShellPolicy")] == ["src/shell_policy.py"]
    rendered = memory.render_file_summaries("ShellPolicy")
    assert "src/shell_policy.py" in rendered
    assert "ShellPolicy" in rendered
    assert "96-148" in rendered


def test_keywords_extract_ascii_terms_and_cjk_bigrams():
    assert _keywords("受控 shell 工具") == {"shell", "受控", "工具"}


def test_chinese_query_recalls_file_summary():
    """A requirement written in Chinese must recall the matching summary."""
    memory = make_memory()
    memory.data["file_summaries"] = {
        "src/shell_policy.py": {"summary": "受控 shell 工具的策略层", "symbols": ["ShellPolicy"], "freshness": "a"},
        "docs/old.md": {"summary": "无关的历史记录", "symbols": [], "freshness": "b"},
    }

    paths = [item["path"] for item in memory.retrieve_file_summaries("策略层在哪")]

    assert paths == ["src/shell_policy.py"]


def test_file_summary_prompt_keeps_ascii_terms_for_recall():
    assert "英文标识符" in FILE_SUMMARY_SYSTEM_PROMPT


def test_note_content_is_recallable_by_chinese_query():
    """Note text must be tokenized on read, so Chinese queries can recall it."""
    memory = make_memory()
    memory.data["episodic_notes"] = [
        {
            "id": "note-1",
            "content": "shell 失败：approval_denied - 用户拒绝了命令",
            "keywords": ["shell", "approval_denied"],
            "created_at": "1",
        }
    ]

    assert [note["id"] for note in memory.retrieve_relevant("命令被拒绝了")] == ["note-1"]


def test_note_recall_keeps_using_stored_keywords():
    """Stored keywords still count: they carry terms absent from the content."""
    memory = make_memory()
    memory.data["episodic_notes"] = [
        {"id": "note-1", "content": "已检查文件 src/foo.py", "keywords": ["readfile", "src", "foo", "py"], "created_at": "1"}
    ]

    assert [note["id"] for note in memory.retrieve_relevant("readfile")] == ["note-1"]


def test_tool_note_is_recallable_by_chinese_query():
    """End to end: the note written by a tool result carries Chinese content.

    The stored keywords are ASCII tool/path names, so only the note text can
    match a Chinese requirement.
    """
    memory = make_memory()
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "src/shell_policy.py"}),
        {"ok": True, "path": "src/shell_policy.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
        run_id="r",
        round_number=1,
        raw_content="x\n",
    )

    assert memory.data["episodic_notes"][0]["keywords"] == ["py", "readfile", "shell_policy", "src"]
    assert [note["id"] for note in memory.retrieve_relevant("检查过的文件有哪些")] == ["note-1"]


def test_error_note_is_not_recalled_by_an_unrelated_query():
    """The latest failure must not be pulled into an unrelated question.

    ``latest_tool_error`` is already rendered into the [Memory] block, so it
    must not also act as a retrieval key: the note built from that error text
    would otherwise always outrank the note the user actually asked about.
    """
    memory = make_memory()
    memory.begin_run("检查一下这个仓库")
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "src/shell_policy.py"}),
        {"ok": True, "path": "src/shell_policy.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
        run_id="r",
        round_number=1,
        raw_content="x\n",
    )
    memory.observe_tool_result(
        ToolCall("2", "shell", {"program": "pytest", "args": []}),
        {"ok": False, "error": {"type": "approval_denied", "message": "用户拒绝了命令"}},
        run_id="r",
        round_number=2,
    )

    assert [note["id"] for note in memory.retrieve_relevant("检查过的文件")] == ["note-1"]


def test_task_summary_still_drives_note_recall():
    """A terse follow-up still recalls notes through the run task summary."""
    memory = make_memory()
    memory.begin_run("检查 shell 策略层")
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "src/shell_policy.py"}),
        {"ok": True, "path": "src/shell_policy.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
        run_id="r",
        round_number=1,
        raw_content="x\n",
    )

    assert [note["id"] for note in memory.retrieve_relevant("继续")] == ["note-1"]


def test_truncated_read_does_not_create_a_file_summary():
    """A partial read must never be summarized as if it were the whole file."""
    memory = make_memory()
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "big.py"}),
        {"ok": True, "path": "big.py", "content": "1: x", "start": 1, "end": 5, "line_count": 20000, "truncated": True},
        run_id="r",
        round_number=1,
        raw_content="x\n",
    )

    assert "big.py" not in memory.data["file_summaries"]


def test_latest_tool_error_is_cleared_by_a_successful_tool_result():
    """A stale failure must not stay in the [Memory] block for the whole run."""
    memory = make_memory()
    memory.observe_tool_result(
        ToolCall("1", "shell", {"program": "pytest", "args": []}),
        {"ok": False, "error": {"type": "approval_denied", "message": "用户拒绝了命令"}},
        run_id="r",
        round_number=1,
    )
    assert memory.data["working_memory"]["latest_tool_error"] is not None

    memory.observe_tool_result(
        ToolCall("2", "readfile", {"path": "a.py"}),
        {"ok": True, "path": "a.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
        run_id="r",
        round_number=2,
        raw_content="x\n",
    )

    assert memory.data["working_memory"]["latest_tool_error"] is None
    assert "latest_tool_error" not in memory.render_memory()


def test_tool_result_summary_provider_returns_validated_observation():
    captured = []

    def complete_text(messages):
        captured.append(messages)
        return '{"observation":"pytest passed 54 tests"}'

    summary = LLMToolResultSummaryProvider(complete_text)("shell", {"ok": True, "stdout": "..."})

    assert summary == {"ok": True, "observation": "pytest passed 54 tests"}
    assert "工具结果压缩器" in captured[0][0]["content"]


def test_tool_result_summary_provider_rejects_invalid_json():
    provider = LLMToolResultSummaryProvider(lambda messages: "not json")

    with pytest.raises(ValueError):
        provider("shell", {"ok": True})
