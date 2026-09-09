from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.shell_policy import (
    ShellPolicy,
    ShellPolicyError,
    ShellRequest,
)


def policy_workspace(sample_git_repo):
    return Workspace(sample_git_repo)


def test_allows_python_script_inside_workspace(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")
    request = ShellRequest("PYTHON", ["script.py"])
    validated = ShellPolicy().validate(request, policy_workspace(sample_git_repo))
    assert validated.program == "python"
    assert validated.args == ["script.py"]
    assert validated.cwd == "."
    assert validated.timeout == 60.0


def test_rejects_unknown_program(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("curl", ["https://example.com"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "command_not_allowed"


def test_rejects_shell_metacharacters(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("python", ["script.py", "&", "whoami"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "unsafe_argument"


def test_rejects_python_c_and_m(sample_git_repo):
    for arguments in (["-c", "print(1)"], ["-m", "pytest"]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("python", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type == "unsafe_argument"


def test_allows_read_only_git_commands_and_rejects_commit(sample_git_repo):
    validated = ShellPolicy().validate(ShellRequest("git", ["status"]), policy_workspace(sample_git_repo))
    assert validated.program == "git"
    assert validated.approval_required is False
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("git", ["commit", "-am", "x"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "subcommand_not_allowed"


def test_non_read_only_commands_require_approval(sample_git_repo):
    (sample_git_repo / "script.py").write_text("print(1)\n", encoding="utf-8")

    validated = ShellPolicy().validate(ShellRequest("python", ["script.py"]), policy_workspace(sample_git_repo))

    assert validated.approval_required is True


def test_allows_npm_test_and_run_but_rejects_install(sample_git_repo):
    assert ShellPolicy().validate(ShellRequest("npm", ["test"]), policy_workspace(sample_git_repo)).program == "npm"
    assert ShellPolicy().validate(ShellRequest("npm", ["run", "lint"]), policy_workspace(sample_git_repo)).args == ["run", "lint"]
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("npm", ["install"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "subcommand_not_allowed"


def test_rejects_path_outside_workspace(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("python", ["..\\outside.py"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "workspace_violation"


def test_rejects_timeout_above_maximum(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("git", ["status"], timeout=301), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "invalid_arguments"


def test_rejects_missing_cwd(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("git", ["status"], cwd="missing"), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "cwd_not_found"


def test_allows_uv_sync_dev(sample_git_repo):
    validated = ShellPolicy().validate(
        ShellRequest("uv", ["sync", "--dev"]), policy_workspace(sample_git_repo)
    )
    assert validated.program == "uv"
    assert validated.args == ["sync", "--dev"]


def test_rejects_other_uv_commands(sample_git_repo):
    for arguments in (["add", "pytest"], ["pip", "install", "pytest"], ["sync"]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("uv", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type == "subcommand_not_allowed"


def test_allows_uv_run_workspace_script(sample_git_repo):
    script = sample_git_repo / "scripts" / "check.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    validated = ShellPolicy().validate(
        ShellRequest("uv", ["run", "scripts/check.py", "--verbose"]), policy_workspace(sample_git_repo)
    )
    assert validated.args == ["run", "scripts/check.py", "--verbose"]


def test_allows_uv_run_pytest_and_python_script(sample_git_repo):
    script = sample_git_repo / "scripts" / "check.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("print('ok')\n", encoding="utf-8")

    pytest_request = ShellPolicy().validate(
        ShellRequest("uv", ["run", "pytest", "tests", "-q"]), policy_workspace(sample_git_repo)
    )
    python_request = ShellPolicy().validate(
        ShellRequest("uv", ["run", "python", "scripts/check.py"]), policy_workspace(sample_git_repo)
    )

    assert pytest_request.approval_required is True
    assert python_request.approval_required is True


def test_rejects_unsafe_uv_run_targets(sample_git_repo):
    for arguments in (["run", "python", "-c", "print(1)"], ["run", "powershell", "x"], ["run"]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("uv", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type in {"subcommand_not_allowed", "unsafe_argument"}
