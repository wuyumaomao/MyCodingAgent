from __future__ import annotations

import pytest

from coding_agent.events import NullEventSink
from coding_agent.repository import Workspace
from coding_agent.tools.approval import event_sink_context
from coding_agent.tools.shell import ShellApprovalGate, ShellPreview, ShellTool
from coding_agent.tools.shell_policy import ShellPolicy
from coding_agent.tools.shell_runner import ShellRunResult


class FakeRunner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or ShellRunResult(0, "ok", "", False, False, 1.0)

    def run(self, request, workspace, max_output_bytes):
        self.calls.append((request, workspace, max_output_bytes))
        return self.result


class Events:
    def __init__(self):
        self.items = []

    def emit(self, event_type, **payload):
        self.items.append((event_type, payload))

    def complete(self, answer):
        pass

    def fail(self, error_type, message, *, duration_ms=None):
        pass


def arguments():
    return {"program": "git", "args": ["status"], "cwd": ".", "timeout": 1}


def python_arguments():
    return {"program": "python", "args": ["script.py"], "cwd": ".", "timeout": 1}


def test_shell_tool_requires_approval_without_starting_runner(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
    runner = FakeRunner()
    tool = ShellTool(
        Workspace(sample_git_repo),
        ShellPolicy(),
        ShellApprovalGate(),
        runner,
    )
    result = tool.execute(python_arguments())
    assert result["error"]["type"] == "approval_required"
    assert runner.calls == []


def test_shell_tool_denial_returns_structured_error(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
    runner = FakeRunner()
    tool = ShellTool(
        Workspace(sample_git_repo), ShellPolicy(), ShellApprovalGate(ask=lambda _: False), runner
    )
    result = tool.execute(python_arguments())
    assert result["error"]["type"] == "approval_denied"
    assert runner.calls == []


def test_shell_tool_approves_and_returns_process_result(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
    runner = FakeRunner(ShellRunResult(0, "clean", "", False, False, 2.5))
    tool = ShellTool(
        Workspace(sample_git_repo), ShellPolicy(), ShellApprovalGate(ask=lambda _: True), runner
    )
    result = tool.execute(python_arguments())
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "clean"
    assert result["duration_ms"] == 2.5
    assert len(runner.calls) == 1


def test_shell_tool_policy_error_does_not_request_approval(sample_git_repo):
    asked = []
    runner = FakeRunner()
    tool = ShellTool(
        Workspace(sample_git_repo), ShellPolicy(), ShellApprovalGate(ask=lambda preview: asked.append(preview) or True), runner
    )
    result = tool.execute({"program": "git", "args": ["commit"], "cwd": ".", "timeout": 1})
    assert result["error"]["type"] == "subcommand_not_allowed"
    assert asked == []
    assert runner.calls == []


def test_shell_tool_emits_approval_events_to_current_sink(sample_git_repo):
    events = Events()
    runner = FakeRunner()
    tool = ShellTool(
        Workspace(sample_git_repo), ShellPolicy(), ShellApprovalGate(ask=lambda _: False), runner
    )
    with event_sink_context(events):
        (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
        result = tool.execute(python_arguments())
    assert result["error"]["type"] == "approval_denied"
    assert [item[0] for item in events.items] == ["approval_request", "approval_result"]
    assert events.items[0][1]["report_payload"]["preview"]["program"] == "python"


def test_shell_tool_auto_approves_read_only_git_query(sample_git_repo):
    events = Events()
    runner = FakeRunner()
    tool = ShellTool(
        Workspace(sample_git_repo),
        ShellPolicy(),
        ShellApprovalGate(ask=lambda _: pytest.fail("read-only git command requested approval")),
        runner,
    )

    with event_sink_context(events):
        result = tool.execute(arguments())

    assert result["ok"] is True
    assert len(runner.calls) == 1
    assert [item[0] for item in events.items] == ["approval_result"]
    assert events.items[0][1]["decision"] == "auto_approved"


def test_shell_tool_reports_timeout_and_truncated_output(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
    runner = FakeRunner(ShellRunResult(1, "out", "err", True, True, 10.0))
    tool = ShellTool(
        Workspace(sample_git_repo), ShellPolicy(), ShellApprovalGate(ask=lambda _: True), runner
    )
    result = tool.execute(python_arguments())
    assert result["ok"] is False
    assert result["error"]["type"] == "timeout"
    assert result["stdout"] == "out"
    assert result["output_truncated"] is True
