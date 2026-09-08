from __future__ import annotations

from pathlib import Path

from coding_agent.benchmark.cli import main


def test_benchmark_cli_fake_mode_succeeds(tmp_path):
    code = main([
        "--mode", "fake",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert code == 0
    assert (tmp_path / "runs" / "fake" / "summary.json").exists()


def test_benchmark_cli_rejects_unknown_mode():
    try:
        code = main(["--mode", "unknown"])
    except SystemExit as exc:
        code = int(exc.code)
    assert code != 0


def test_benchmark_cli_real_mode_requires_configuration(tmp_path, monkeypatch):
    from coding_agent.config import ConfigError

    monkeypatch.setattr(
        "coding_agent.benchmark.cli.Settings.from_args_and_env",
        lambda _: (_ for _ in ()).throw(ConfigError("API key is required")),
    )
    code = main(["--mode", "real", "--runs-root", str(tmp_path / "runs")])
    assert code != 0
