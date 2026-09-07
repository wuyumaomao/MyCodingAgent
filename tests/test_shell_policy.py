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
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("git", ["commit", "-am", "x"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "subcommand_not_allowed"


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
