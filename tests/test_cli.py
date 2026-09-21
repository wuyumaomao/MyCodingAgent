from __future__ import annotations

from argparse import Namespace
import json

from coding_agent.cli import main
from coding_agent.config import Settings
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.tools.approval import WritePreview
from coding_agent.tools.shell import ShellPreview
from coding_agent.tools.shell_runner import ShellRunResult


class FakeClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        return self.turns.pop(0)


def test_cli_requires_query(capsys):
    assert main([]) != 0
    assert "usage:" in capsys.readouterr().err


def test_settings_use_cli_over_environment(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://env.example/v1")
    settings = Settings.from_args_and_env(
        Namespace(api_key=None, model="cli-model", base_url=None)
    )
    assert settings.api_key == "env-key"
    assert settings.model == "cli-model"
    assert settings.base_url == "https://env.example/v1"


def test_settings_load_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("CODING_AGENT_API_KEY", "CODING_AGENT_MODEL", "CODING_AGENT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "CODING_AGENT_API_KEY=file-key\n"
        "CODING_AGENT_MODEL=file-model\n"
        "CODING_AGENT_BASE_URL=https://file.example/v1\n",
        encoding="utf-8",
    )
    settings = Settings.from_args_and_env(Namespace(api_key=None, model=None, base_url=None))
    assert settings.api_key == "file-key"
    assert settings.model == "file-model"
    assert settings.base_url == "https://file.example/v1"


def test_settings_parse_timeout(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    monkeypatch.setenv("CODING_AGENT_TIMEOUT", "12.5")
    settings = Settings.from_args_and_env(
        Namespace(api_key=None, model=None, base_url=None, timeout=None)
    )
    assert settings.timeout == 12.5


def test_settings_parse_context_window_tokens(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    monkeypatch.setenv("CODING_AGENT_CONTEXT_WINDOW_TOKENS", "128000")
    settings = Settings.from_args_and_env(
        Namespace(api_key=None, model=None, base_url=None, timeout=None)
    )
    assert settings.context_window_tokens == 128000


def test_settings_reject_non_positive_context_window_tokens(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    with __import__("pytest").raises(ValueError, match="positive"):
        Settings.from_args_and_env(
            Namespace(api_key=None, model=None, base_url=None, timeout=None, context_window_tokens=0)
        )


def test_settings_default_model_timeout_is_600_seconds(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")

    settings = Settings.from_args_and_env(
        Namespace(api_key=None, model=None, base_url=None, timeout=None)
    )

    assert settings.timeout == 600.0


def test_settings_reject_non_positive_timeout(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "env-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    with __import__("pytest").raises(ValueError, match="positive"):
        Settings.from_args_and_env(
            Namespace(api_key=None, model=None, base_url=None, timeout=0)
        )


def test_cli_uses_repo_argument_and_renders_answer(monkeypatch, sample_git_repo, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr(
        "coding_agent.cli.AgentService.run",
        lambda self, query, workspace: "ok",
    )
    assert main(["Explain scripts", "--repo", str(sample_git_repo)]) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_cli_returns_error_when_configuration_is_missing(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODING_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("CODING_AGENT_MODEL", raising=False)
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    assert main(["Explain scripts", "--repo", str(sample_git_repo)]) != 0
    captured = capsys.readouterr()
    assert "API key" in captured.err
    trace_files = list((tmp_path / "runs").glob("*/trace.json"))
    assert len(trace_files) == 1
    assert __import__("json").loads(trace_files[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_cli_creates_trace_for_success(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: FakeClient([AssistantTurn("ok", [])]))
    assert main(["Explain", "--repo", str(sample_git_repo)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "ok"
    assert "Run:" in captured.err
    assert "Trace:" in captured.err
    trace_files = list((tmp_path / "runs").glob("*/trace.json"))
    assert len(trace_files) == 1
    assert json.loads(trace_files[0].read_text(encoding="utf-8"))["status"] == "completed"


def test_cli_creates_failed_trace(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    def fail_client(**kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr("coding_agent.cli.LLMClient", fail_client)
    assert main(["Explain", "--repo", str(sample_git_repo)]) != 0
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    trace_files = list((tmp_path / "runs").glob("*/trace.json"))
    assert len(trace_files) == 1
    assert json.loads(trace_files[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_cli_runs_from_nested_directory_with_fake_provider(monkeypatch, sample_git_repo, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    client = FakeClient(
        [
            AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "package.json"})]),
            AssistantTurn("The repository uses npm scripts.", []),
        ]
    )
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: client)
    assert main(["Explain scripts", "--repo", str(sample_git_repo / "src")]) == 0
    assert "npm scripts" in capsys.readouterr().out
    assert any(message[1]["content"].startswith("[Repository Context]") for message in client.messages)


def test_cli_outputs_report_path(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        "coding_agent.cli.LLMClient",
        lambda **kwargs: FakeClient([AssistantTurn("ok", [])]),
    )
    assert main(["Explain", "--repo", str(sample_git_repo)]) == 0
    assert "Report:" in capsys.readouterr().err
    report = next((tmp_path / "runs").glob("*/report.json"))
    document = json.loads(report.read_text(encoding="utf-8"))
    request = next(event for event in document["events"] if event["type"] == "llm_request")
    assert "messages" in request


def test_cli_passes_timeout_to_llm_client(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    received = {}

    def make_client(**kwargs):
        received.update(kwargs)
        return FakeClient([AssistantTurn("ok", [])])

    monkeypatch.setattr("coding_agent.cli.LLMClient", make_client)
    assert main(["Explain", "--timeout", "12", "--repo", str(sample_git_repo)]) == 0
    assert received["timeout"] == 12.0


def test_cli_passes_max_tool_calls(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    captured = {}

    def make_service(loop):
        captured["limit"] = loop.limits.max_calls_per_tool
        return type("Service", (), {"run": lambda self, query, workspace: "ok"})()

    monkeypatch.setattr("coding_agent.cli.AgentService", make_service)
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: FakeClient([]))
    assert main(["Explain", "--max-tool-calls", "5", "--repo", str(sample_git_repo)]) == 0
    assert captured["limit"] == 5


def test_ask_write_approval_accepts_only_yes(monkeypatch, sample_git_repo):
    from coding_agent.cli import _ask_write_approval

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert _ask_write_approval(WritePreview("create", "new.py", content="hello")) is True

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _ask_write_approval(WritePreview("create", "new.py", content="hello")) is True

    monkeypatch.setattr("builtins.input", lambda _: "yes please")
    assert _ask_write_approval(WritePreview("create", "new.py", content="hello")) is False


def test_cli_shell_approval_accepts_only_yes(monkeypatch):
    from coding_agent.cli import _ask_shell_approval

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert _ask_shell_approval(ShellPreview("git", ["status"], ".", 60)) is True
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert _ask_shell_approval(ShellPreview("git", ["status"], ".", 60)) is False


def test_cli_passes_shell_timeout_to_policy(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    captured = {}

    class Agent:
        def ask(self, query, *, recorder):
            return "ok"

    def create_agent(repo, settings, **kwargs):
        captured.update(kwargs)
        return Agent()

    monkeypatch.setattr("coding_agent.cli.CodingAgent.from_settings", create_agent)
    assert main(["status", "--shell-timeout", "12", "--repo", str(sample_git_repo)]) == 0
    assert captured["shell_policy"].default_timeout == 12.0


def test_cli_shell_command_runs_after_approval(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    client = FakeClient([
        AssistantTurn(None, [ToolCall("shell-1", "shell", {"program": "pytest", "args": ["tests"]})]),
        AssistantTurn("checked", []),
    ])
    calls = []

    class Runner:
        def run(self, request, workspace, max_output_bytes):
            calls.append(request)
            return ShellRunResult(0, "clean", "", False, False, 1.0)

    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: client)
    monkeypatch.setattr("coding_agent.tools.shell.WindowsProcessRunner", Runner)
    assert main(["check", "--repo", str(sample_git_repo)]) == 0
    assert capsys.readouterr().out.strip() == "checked"
    assert len(calls) == 1
    # 记忆快照现在是 prompt 的最后一条消息；工具结果在其之前
    tool_messages = [m for m in client.messages[-1] if m.get("role") == "tool"]
    assert tool_messages and "exit_code" in tool_messages[-1]["content"]


def test_cli_shell_command_is_rejected_without_tty(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    client = FakeClient([
        AssistantTurn(None, [ToolCall("shell-1", "shell", {"program": "pytest", "args": ["tests"]})]),
        AssistantTurn("approval needed", []),
    ])
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: client)
    assert main(["check", "--repo", str(sample_git_repo)]) == 0
    assert capsys.readouterr().out.strip() == "approval needed"
    tool_messages = [m for m in client.messages[-1] if m.get("role") == "tool"]
    assert tool_messages and "approval_required" in tool_messages[-1]["content"]


def test_cli_write_tool_requires_approval_and_can_write(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr(
        "coding_agent.cli.LLMClient",
        lambda **kwargs: FakeClient([
            AssistantTurn(None, [ToolCall("write-1", "write_file", {"path": "new.py", "content": "ok"})]),
            AssistantTurn("written", []),
        ]),
    )

    assert main(["Create", "file", "--repo", str(sample_git_repo)]) == 0
    assert (sample_git_repo / "new.py").read_text(encoding="utf-8") == "ok"


def test_cli_write_tool_denied_does_not_modify(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr(
        "coding_agent.cli.LLMClient",
        lambda **kwargs: FakeClient([
            AssistantTurn(None, [ToolCall("write-1", "write_file", {"path": "new.py", "content": "ok"})]),
            AssistantTurn("not written", []),
        ]),
    )

    assert main(["Create", "file", "--repo", str(sample_git_repo)]) == 0
    assert not (sample_git_repo / "new.py").exists()
    assert "not written" in capsys.readouterr().out
