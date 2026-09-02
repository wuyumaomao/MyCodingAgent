from __future__ import annotations

from coding_agent.models import AssistantTurn, ToolCall

from coding_agent.context import ConversationContext, build_repository_context
from coding_agent.repository import Workspace


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
    assert "name = 'sample'" in context
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


def test_context_truncates_large_important_file(sample_git_repo):
    large_content = "x" * (12 * 1024) + "TAIL_MARKER"
    (sample_git_repo / "README.md").write_text(large_content, encoding="utf-8")

    context = build_repository_context(Workspace(sample_git_repo))

    assert "### README.md" in context
    assert "truncated: true" in context
    assert "TAIL_MARKER" not in context


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
