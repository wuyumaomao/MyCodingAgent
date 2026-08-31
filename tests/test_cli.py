from __future__ import annotations

from argparse import Namespace

from coding_agent.cli import main
from coding_agent.config import Settings
from coding_agent.models import AssistantTurn, ToolCall


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


def test_cli_uses_repo_argument_and_renders_answer(monkeypatch, sample_git_repo, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr(
        "coding_agent.cli.AgentService.run",
        lambda self, query, workspace: "ok",
    )
    assert main(["Explain scripts", "--repo", str(sample_git_repo)]) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_cli_returns_error_when_configuration_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("CODING_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("CODING_AGENT_MODEL", raising=False)
    assert main(["Explain scripts"]) != 0
    assert "API key" in capsys.readouterr().err


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
