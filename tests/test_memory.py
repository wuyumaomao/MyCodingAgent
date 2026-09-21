import json
from pathlib import Path

import pytest

from coding_agent.memory import (
    FILE_SUMMARY_SYSTEM_PROMPT,
    LLMFileSummaryProvider,
    MemoryManager,
    _keywords,
    fallback_symbols,
)
from coding_agent.models import ToolCall
from coding_agent.session import SessionState, empty_memory


def make_memory():
    return MemoryManager(SessionState("session-1", Path("C:/repo"), memory=empty_memory()))


def test_full_read_creates_summary_and_recent_file():
    memory = make_memory()
    memory.observe_tool_result(ToolCall("1", "readfile", {"path": "a.py"}), {"ok": True, "path": "a.py", "content": "1: def main():\n2:     pass", "start": 1, "end": 2, "line_count": 2}, run_id="r", round_number=1)
    assert memory.data["working_memory"]["recent_read_files"] == ["a.py"]
    assert len(memory.data["file_summaries"]["a.py"]["freshness"]) == 64


def test_listing_a_directory_does_not_count_as_reading_a_file():
    """`recent_files` 曾经把 listfiles/find_files 结果里的目录也记进来。

    于是这一栏会变成 "httpstat.py, kb, tests, ."——目录混在文件里，渲染出去
    就是一行噪音。只有真正用 readfile 读过内容的路径才算"读过"。
    """
    memory = make_memory()
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "a.py"}),
        {"ok": True, "path": "a.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
        run_id="r",
        round_number=1,
    )
    memory.observe_tool_result(
        ToolCall("2", "listfiles", {"path": "."}),
        {"ok": True, "path": ".", "files": ["a.py", "kb"], "truncated": False},
        run_id="r",
        round_number=1,
    )
    memory.observe_tool_result(
        ToolCall("3", "find_files", {"pattern": "*.py"}),
        {"ok": True, "path": "kb", "files": ["kb/b.py"], "truncated": False},
        run_id="r",
        round_number=1,
    )
    memory.observe_tool_result(
        ToolCall("4", "readfile", {"path": "missing.py"}),
        {"ok": False, "error": {"type": "file_not_found", "message": "File not found"}},
        run_id="r",
        round_number=1,
    )

    assert memory.data["working_memory"]["recent_read_files"] == ["a.py"]


def test_memory_block_renders_the_files_the_model_read():
    """读过哪些文件必须发进 prompt。

    历史里的 readfile 结果会被段落压缩吃掉，[Memory] 不会被——所以这一栏是压缩
    之后模型唯一还能知道"这个文件我已经读过了"的地方。
    """
    memory = make_memory()
    for index, path in enumerate(("a.py", "b.py")):
        memory.observe_tool_result(
            ToolCall(str(index), "readfile", {"path": path}),
            {"ok": True, "path": path, "content": "1: x", "start": 1, "end": 1, "line_count": 1},
            run_id="r",
            round_number=1,
        )

    rendered = memory.render_memory()

    assert "recent_read_files: b.py, a.py" in rendered
    assert "recent_modified_files: none" in rendered


def test_recent_read_files_is_capped_and_moves_the_latest_to_the_front():
    """最近读的排最前，且这一栏不能无限长（它每轮都要重发）。"""
    memory = make_memory()
    for index in range(14):
        path = f"f{index}.py"
        memory.observe_tool_result(
            ToolCall(str(index), "readfile", {"path": path}),
            {"ok": True, "path": path, "content": "1: x", "start": 1, "end": 1, "line_count": 1},
            run_id="r",
            round_number=1,
        )

    recorded = memory.data["working_memory"]["recent_read_files"]

    assert len(recorded) == 10
    assert recorded[0] == "f13.py"
    assert "f0.py" not in recorded


def test_patch_invalidates_summary():
    memory = make_memory()
    memory.data["file_summaries"]["a.py"] = {"freshness": "x"}
    memory.data["compaction"] = {
        "covered": 12,
        "summary": "[Context Handoff]\nold a.py conclusion",
        "ledger": {"read": {"a.py": {"whole": True, "lines": 10, "ranges": []}}},
    }
    memory.observe_tool_result(ToolCall("1", "patch_file", {"path": "a.py"}), {"ok": True, "path": "a.py"}, run_id="r", round_number=1)
    assert "a.py" not in memory.data["file_summaries"]
    assert memory.data["compaction"]["covered"] == 0
    assert memory.data["compaction"]["summary"] == ""
    assert memory.data["compaction"]["ledger"] == {}


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
    """A note whose keywords are ASCII-only is recalled through its Chinese text."""
    memory = make_memory()
    memory.data["episodic_notes"] = [
        {
            "id": "note-1",
            "content": "已检查文件 src/shell_policy.py",
            "keywords": ["py", "readfile", "shell_policy", "src"],
            "created_at": "1",
        }
    ]

    assert [note["id"] for note in memory.retrieve_relevant("检查过的文件有哪些")] == ["note-1"]


def test_error_note_is_not_recalled_by_an_unrelated_query():
    """The latest failure must not be pulled into an unrelated question.

    ``latest_tool_error`` is already rendered into the [Memory] block, so it
    must not also act as a retrieval key: the note built from that error text
    would otherwise always outrank the note the user actually asked about.
    """
    memory = make_memory()
    memory.begin_run("检查一下这个仓库")
    memory.data["episodic_notes"] = [
        {"id": "note-1", "content": "已检查文件 src/shell_policy.py", "keywords": ["py", "readfile", "shell_policy", "src"], "created_at": "1"},
        {"id": "note-2", "content": "shell 失败：approval_denied - 用户拒绝了命令", "keywords": ["shell", "approval_denied"], "created_at": "2"},
    ]

    assert [note["id"] for note in memory.retrieve_relevant("检查过的文件")] == ["note-1"]


def test_task_summary_still_drives_note_recall():
    """A terse follow-up still recalls notes through the run task summary."""
    memory = make_memory()
    memory.begin_run("检查 shell 策略层")
    memory.data["episodic_notes"] = [
        {"id": "note-1", "content": "已检查文件 src/shell_policy.py", "keywords": ["py", "readfile", "shell_policy", "src"], "created_at": "1"},
    ]

    assert [note["id"] for note in memory.retrieve_relevant("继续")] == ["note-1"]


def test_episodic_notes_are_frozen():
    """Note writing is frozen: tool results no longer create notes.

    The recall path stays alive so sessions written before the freeze keep
    working.
    """
    memory = make_memory()
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "a.py"}),
        {"ok": True, "path": "a.py", "content": "1: x", "start": 1, "end": 1, "line_count": 1},
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

    assert memory.data["episodic_notes"] == []

    memory.data["episodic_notes"].append(
        {"id": "note-1", "content": "已检查文件 a.py", "keywords": ["readfile"], "created_at": "1"}
    )
    assert [note["id"] for note in memory.retrieve_relevant("readfile")] == ["note-1"]


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


def test_tool_result_summary_provider_is_gone():
    """单个工具结果不再走 LLM 压缩：确定性优先，只保留段落交接摘要。"""
    import coding_agent.memory as memory_module

    assert not hasattr(memory_module, "LLMToolResultSummaryProvider")
    assert hasattr(memory_module, "LLMSpanSummaryProvider")
    assert hasattr(memory_module, "LLMFileSummaryProvider")


def test_file_summary_provider_tolerates_fences_and_prose():
    """模型多包一层代码块或前后多说一句，不该让整份摘要降级。"""
    payload = '{"summary":"命令行入口","symbols":["main"],"line_index":[{"lines":"1-2","desc":"入口"}]}'

    for raw in (f"```json\n{payload}\n```", f"好的，这是摘要：\n{payload}", f"```\n{payload}\n```\n希望有帮助。"):
        provider = LLMFileSummaryProvider(lambda messages, raw=raw: raw)
        summary = provider("a.py", "def main(): pass", {})
        assert summary["summary"] == "命令行入口"
        assert summary["symbols"] == ["main"]


def test_file_summary_provider_treats_null_as_empty():
    """模型用 null 表示"没有符号"是合法答案，不是错误。"""
    provider = LLMFileSummaryProvider(lambda messages: '{"summary":"规划文档","symbols":null,"line_index":null}')

    summary = provider("AGENTS.md", "# 规划\n", {})

    assert summary["summary"] == "规划文档"
    assert summary["symbols"] == []
    assert summary["line_index"] == []


def test_file_summary_provider_still_rejects_a_missing_summary():
    provider = LLMFileSummaryProvider(lambda messages: '{"symbols":["a"]}')

    with pytest.raises(ValueError):
        provider("a.py", "x", {})


def test_line_index_accepts_common_key_variants():
    """模型可能写成 range/description，或拆成 start+end；这些都该被接受。"""
    raw = json.dumps({
        "summary": "入口",
        "symbols": [{"name": "main"}],
        "line_index": [
            {"range": "1-7", "description": "导入"},
            {"start": 12, "end": 24, "desc": "parse_bool"},
            {"line": "30", "summary": "pop_arg"},
        ],
    }, ensure_ascii=False)
    provider = LLMFileSummaryProvider(lambda messages: raw)

    summary = provider("a.py", "x", {})

    assert summary["symbols"] == ["main"]
    assert summary["line_index"] == [
        {"lines": "1-7", "desc": "导入"},
        {"lines": "12-24", "desc": "parse_bool"},
        {"lines": "30", "desc": "pop_arg"},
    ]


def test_malformed_line_index_entries_are_dropped_not_fatal():
    """一个条目格式不对，不该让整份摘要（含正确的 summary 和 symbols）作废。"""
    raw = json.dumps({
        "summary": "命令行入口",
        "symbols": ["main", "parse_slo"],
        "line_index": [
            {"lines": "1-7", "desc": "导入"},
            "这不是对象",
            {"range": "12-24"},
            {"lines": "30-40", "desc": "check_slo"},
        ],
    }, ensure_ascii=False)
    provider = LLMFileSummaryProvider(lambda messages: raw)

    summary = provider("a.py", "x", {})

    assert summary["summary"] == "命令行入口"
    assert summary["symbols"] == ["main", "parse_slo"]
    assert summary["line_index"] == [{"lines": "1-7", "desc": "导入"}, {"lines": "30-40", "desc": "check_slo"}]


def test_fallback_summary_extracts_symbols_without_the_model():
    """降级也要保住符号召回能力。"""
    content = "import os\n\nclass Env:\n    pass\n\ndef main():\n    pass\n\n    def helper():\n        pass\n"

    symbols = fallback_symbols(content)

    assert symbols[:3] == ["Env", "main", "helper"]


def test_failed_file_summary_is_visible_and_marked():
    """失败必须留下事件，并且降级产物要能被识别出来。"""
    events = []

    class Sink:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    def broken(path, content, result):
        raise ValueError("FileSummary response was not valid JSON")

    memory = MemoryManager(make_memory().session, summary_provider=broken, event_sink=Sink())
    memory.observe_tool_result(
        ToolCall("1", "readfile", {"path": "a.py"}),
        {"ok": True, "path": "a.py", "content": "1: class Env:", "start": 1, "end": 3, "line_count": 3},
        run_id="r",
        round_number=1,
        raw_content="class Env:\n    pass\n\ndef main():\n    pass\n",
    )

    failures = [payload for event_type, payload in events if payload.get("kind") == "file_summary_failed"]
    assert failures and failures[0]["path"] == "a.py"

    stored = memory.data["file_summaries"]["a.py"]
    assert stored["fallback"] is True
    assert stored["symbols"] == ["Env", "main"]

    memory.data["working_memory"]["task_summary"] = "查看 Env"
    rendered = memory.render_file_summaries("Env")
    assert "[fallback summary" in rendered
