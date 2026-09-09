from coding_agent.repository import Workspace
from coding_agent.tools.target_environment import resolve_target_python, target_venv_bin


def test_resolves_windows_target_python(sample_git_repo):
    executable = sample_git_repo / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    assert resolve_target_python(Workspace(sample_git_repo), platform_name="nt") == executable
    assert target_venv_bin(Workspace(sample_git_repo), platform_name="nt") == executable.parent


def test_resolves_unix_target_python(sample_git_repo):
    executable = sample_git_repo / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    assert resolve_target_python(Workspace(sample_git_repo), platform_name="posix") == executable


def test_missing_target_python_returns_none(sample_git_repo):
    assert resolve_target_python(Workspace(sample_git_repo), platform_name="nt") is None
