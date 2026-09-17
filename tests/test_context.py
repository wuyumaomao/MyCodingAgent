from __future__ import annotations

import json

import pytest

from coding_agent.models import AssistantTurn, ToolCall

from coding_agent.context import ConversationContext, build_repository_context
from coding_agent.repository import Workspace
from coding_agent.memory import MemoryManager
from coding_agent.session import SessionStore


def test_context_contains_root_and_relative_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo))
    assert "Repository Context" in context
    assert "README.md" in context
    assert str(sample_git_repo) not in context


def test_context_marks_truncated_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo), max_entries=1)
    assert "truncated: true" in context


def test_context_includes_important_files_entrypoint_env_and_git_status(sample_git_repo):
    (sample_git_repo / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    (sample_git_repo / "AGENTS.md").write_text("Keep changes focused.\n", encoding="utf-8")
    (sample_git_repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (sample_git_repo / ".env.example").write_text("API_KEY=replace-me\n", encoding="utf-8")

    context = build_repository_context(Workspace(sample_git_repo))

    assert "[Important Files]" in context
    assert "### pyproject.toml" in context
    assert "content:" not in context
    assert "status: present" in context
    assert "### AGENTS.md" in context
    assert "### main.py" in context
    assert "### .env.example" in context
    assert "[Git Status]" in context
    assert "AGENTS.md" in context


def test_context_reports_missing_important_files_without_failing(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo))

    assert "### pyproject.toml" in context
    assert "status: missing" in context
    assert "### AGENTS.md" in context
    assert "### .env.example" in context


def test_context_does_not_embed_large_important_file(sample_git_repo):
    large_content = "x" * (12 * 1024) + "TAIL_MARKER"
    (sample_git_repo / "README.md").write_text(large_content, encoding="utf-8")

    context = build_repository_context(Workspace(sample_git_repo))

    assert "### README.md" in context
    assert "status: present" in context
    assert "truncated: true" not in context
    assert "TAIL_MARKER" not in context


def test_context_is_navigation_map_without_full_file_manifest(sample_git_repo):
    (sample_git_repo / "random-module.py").write_text("print('noise')\n", encoding="utf-8")

    context = build_repository_context(Workspace(sample_git_repo))
    navigation_map = context.split("[Important Files]", maxsplit=1)[0]

    assert "important_files:" in context
    assert "- README.md" in context
    assert "candidate_dirs:" in context
    assert "- src/" in context
    assert "- tests/" in context
    assert "ignored_runtime_dirs:" in context
    assert ".coding-agent/" in context
    # The full Git-status section may legitimately report an untracked file;
    # it must not be included as part of the initial repository navigation map.
    assert "random-module.py" not in navigation_map
    # A standalone ``files:`` manifest header must not be present; the
    # navigation key ``important_files:`` is intentionally retained.
    assert "\nfiles:\n" not in navigation_map


def test_context_keeps_static_system_messages_and_appends_history(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))
    context.add_user_request("Explain scripts")
    turn = AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True})

    messages = context.messages()
    assert [message["role"] for message in messages[-3:]] == ["user", "assistant", "tool"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert len(context.system_messages) == 2
    assert len(context.history) == 3


def test_context_instructs_model_to_use_write_tools_for_approval(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))

    prompt = context.system_messages[0]["content"]

    assert "directly call write_file or patch_file" in prompt
    assert "Do not ask for approval in ordinary text" in prompt
    assert "tools automatically request user approval" in prompt
    assert "multiple tool calls in the same response" in prompt


def test_context_messages_returns_copy(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))
    context.add_user_request("Explain")
    messages = context.messages()
    messages.append({"role": "user", "content": "extra"})
    assert len(context.messages()) == 3


def test_context_messages_does_not_expose_internal_message_dicts(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))
    context.add_user_request("Explain")
    messages = context.messages()
    messages[0]["content"] = "changed"
    assert context.messages()[0]["content"] != "changed"


def test_repository_context_reports_target_python_environment(sample_git_repo):
    venv_python = sample_git_repo / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    context = build_repository_context(Workspace(sample_git_repo))
    assert "[Runtime Environment]" in context
    assert "target_venv: present" in context
    assert "uv sync --dev" in context


def test_repository_instructions_are_injected_as_a_static_system_message(sample_git_repo):
    (sample_git_repo / "AGENTS.md").write_text(
        "Run tests with: uv run pytest -q\nNever edit generated files.\n", encoding="utf-8"
    )

    context = ConversationContext(Workspace(sample_git_repo))
    context.add_user_request("inspect")

    declared = [m for m in context.system_messages if "Run tests with" in str(m.get("content"))]
    assert len(declared) == 1
    assert declared[0]["role"] == "system"
    assert "[Repository Instructions]" in declared[0]["content"]
    # Static prefix: it is part of system_messages, so it is not rebuilt per round.
    assert "Run tests with" in json.dumps(context.messages(), ensure_ascii=False)


def test_missing_repository_instructions_add_no_message(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))

    assert len(context.system_messages) == 2


def test_prompt_metrics_follow_the_memory_blocks_when_instructions_are_present(sample_git_repo):
    (sample_git_repo / "AGENTS.md").write_text("Run tests with: uv run pytest -q\n", encoding="utf-8")
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    memory.begin_run("inspect")
    context = ConversationContext(Workspace(sample_git_repo), memory=memory)
    context.add_user_request("inspect")
    messages = context.messages("inspect")

    metrics = context.prompt_metrics(messages)

    first_memory_block = messages[len(context.system_messages)]
    assert "[Memory]" in first_memory_block["content"]
    assert metrics["memory_chars"] == len(first_memory_block["content"])


def test_repository_instructions_are_capped_at_the_byte_limit(sample_git_repo):
    (sample_git_repo / "AGENTS.md").write_text("x" * 40000, encoding="utf-8")

    context = ConversationContext(Workspace(sample_git_repo))

    block = next(m["content"] for m in context.system_messages if "[Repository Instructions]" in m["content"])
    assert "truncated" in block
    assert len(block.encode("utf-8")) < 40000


def test_repository_instructions_ignore_symlinks(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("leaked", encoding="utf-8")
    link = sample_git_repo / "AGENTS.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    context = ConversationContext(Workspace(sample_git_repo))

    assert len(context.system_messages) == 2


def test_context_with_memory_renders_sections_and_compresses_old_results(sample_git_repo):
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    memory.begin_run("inspect pytest")
    context = ConversationContext(Workspace(sample_git_repo), memory=memory, transcript_budget_chars=500)
    context.add_user_request("inspect pytest")
    turn = AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True, "path": "README.md", "content": "x" * 10000, "start": 1, "end": 2, "line_count": 2})
    messages = context.messages("inspect pytest")
    assert "[Memory]" in messages[2]["content"]
    assert "[Relevant Memory]" in messages[3]["content"]
    assert all(m.get("role") != "tool" or any(c.get("id") == m.get("tool_call_id") for a in messages if a.get("role") == "assistant" for c in a.get("tool_calls", [])) for m in messages)


def test_context_injects_only_relevant_file_summaries(sample_git_repo):
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    memory.data["file_summaries"] = {
        "src/app.py": {"summary": "CLI entrypoint", "symbols": ["main"], "freshness": "a"},
        "docs/old.md": {"summary": "unrelated notes", "symbols": [], "freshness": "b"},
    }
    context = ConversationContext(Workspace(sample_git_repo), memory=memory)
    messages = context.messages("explain app.py main")
    rendered = "\n".join(str(message.get("content", "")) for message in messages)
    assert "[Relevant File Summaries]" in rendered
    assert "src/app.py" in rendered
    assert "docs/old.md" not in rendered


def test_transcript_compacts_large_tool_result_to_single_result_budget(sample_git_repo):
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    context = ConversationContext(Workspace(sample_git_repo), memory=memory, transcript_budget_chars=10000, tool_result_budget_chars=120)
    context.add_user_request("inspect")
    turn = AssistantTurn(None, [ToolCall("c1", "shell", {"program": "python", "args": ["-c", "print('x')"]})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True, "stdout": "x" * 5000, "stderr": "y" * 5000, "exit_code": 0})
    messages = context.messages()
    tool = next(message for message in messages if message.get("role") == "tool")
    assert len(tool["content"]) <= 120


def test_oversized_tool_result_can_use_injected_summary_provider(sample_git_repo):
    calls = []

    def provider(name, result):
        calls.append(name)
        return {"ok": True, "observation": "tests passed", "exit_code": result.get("exit_code")}

    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    context = ConversationContext(Workspace(sample_git_repo), memory=memory, transcript_budget_chars=10000, tool_result_budget_chars=120, result_summary_provider=provider)
    context.add_user_request("inspect")
    turn = AssistantTurn(None, [ToolCall("c1", "shell", {"program": "python", "args": []})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True, "stdout": "x" * 5000, "exit_code": 0})
    messages = context.messages()
    tool = next(message for message in messages if message.get("role") == "tool")
    assert calls == ["shell"]
    assert json.loads(tool["content"])["observation"] == "tests passed"


def test_repeated_prompts_reuse_one_tool_result_summary(sample_git_repo):
    """Rebuilding the prompt must not re-run the summarizer for the same result."""
    calls = []

    def provider(name, result):
        calls.append(name)
        return {"ok": True, "observation": "tests passed"}

    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    context = ConversationContext(Workspace(sample_git_repo), memory=memory, transcript_budget_chars=10000, tool_result_budget_chars=120, result_summary_provider=provider)
    context.add_user_request("inspect")
    turn = AssistantTurn(None, [ToolCall("c1", "shell", {"program": "python", "args": []})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True, "stdout": "x" * 5000, "exit_code": 0})

    context.messages()
    context.messages()
    context.messages()

    assert calls == ["shell"]


def test_current_request_is_never_dropped_by_transcript_budget(sample_git_repo):
    """Trimming old history must not remove the request the model is answering."""
    query = "请解释这个仓库的启动流程和测试命令，并说明你依据了哪些文件与行号。" * 8
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    memory.begin_run(query)
    context = ConversationContext(Workspace(sample_git_repo), memory=memory, transcript_budget_chars=1000)
    context.add_user_request(query)
    for index in range(6):
        turn = AssistantTurn(None, [ToolCall(f"c{index}", "readfile", {"path": "README.md"})])
        context.add_assistant_turn(turn)
        context.add_tool_result(turn.tool_calls[0], {"ok": True, "path": "README.md", "content": "x" * 100})

    messages = context.messages(query)

    assert any(
        message.get("role") == "user" and message.get("content") == query for message in messages
    ), "the current user request was trimmed out of the prompt"


def test_current_request_is_not_duplicated_when_it_survives_trimming(sample_git_repo):
    session = SessionStore(Workspace(sample_git_repo)).create()
    memory = MemoryManager(session)
    memory.begin_run("inspect")
    context = ConversationContext(Workspace(sample_git_repo), memory=memory)
    context.add_user_request("inspect")

    messages = context.messages("inspect")

    assert [m.get("content") for m in messages if m.get("role") == "user"] == ["inspect"]
