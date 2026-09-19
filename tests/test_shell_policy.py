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


def test_rejection_messages_point_at_the_alternatives(sample_git_repo):
    """拒绝消息必须可行动：模型常想用 | 做正则交替，只回一句"含危险字符"
    它会反复重试同一个思路。

    `python -c` 现在不再拒绝了（走审批），所以这里断言的是**剩下那些**拒绝路径：
    管道仍然拦，且消息里要给出替代工具；`python -m` 仍然拦，消息里要说明可用写法。
    """
    workspace = policy_workspace(sample_git_repo)

    with pytest.raises(ShellPolicyError) as pipe:
        ShellPolicy().validate(ShellRequest("git", ["grep", "-n", "a|b"]), workspace)
    assert pipe.value.error_type == "unsafe_argument"
    assert "search tool" in pipe.value.public_message

    with pytest.raises(ShellPolicyError) as module:
        ShellPolicy().validate(ShellRequest("python", ["-m", "http.server"]), workspace)
    assert module.value.error_type == "unsafe_argument"
    assert "-c" in module.value.public_message
    assert "readfile" in module.value.public_message


def test_rejects_shell_metacharacters(sample_git_repo):
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(ShellRequest("python", ["script.py", "&", "whoami"]), policy_workspace(sample_git_repo))
    assert raised.value.error_type == "unsafe_argument"


def test_python_inline_code_is_allowed_but_requires_approval(sample_git_repo):
    """`python -c` 走审批门，不再被策略硬拒。

    硬拒的代价是模型会绕路：为了跑一行 `os.makedirs`，它往仓库里写了两个临时脚本
    再删掉，花掉 6 轮。审批门本来就是给这种"能力大但需要人看一眼"的命令准备的。
    """
    validated = ShellPolicy().validate(
        ShellRequest("python", ["-c", "print(1)"]),
        policy_workspace(sample_git_repo),
    )

    assert validated.program == "python"
    assert validated.args == ["-c", "print(1)"]
    assert validated.approval_required is True


def test_python_inline_code_may_contain_shell_metacharacters(sample_git_repo):
    """代码参数是数据，不经过任何 shell，所以分号/竖线/重定向必须放行。

    runner 用 `shell=False` + argv 列表启动进程，代码里的 `;` 由 Python 解释，
    不是命令分隔符。只跳过那个代码参数：cwd 和额外的程序参数照旧严查。
    """
    code = "import os; os.makedirs('a')\nprint(1 | 2)\nopen('b','w').write('x')"
    validated = ShellPolicy().validate(
        ShellRequest("python", ["-c", code]),
        policy_workspace(sample_git_repo),
    )

    assert validated.args[1] == code


def test_metacharacters_outside_the_inline_code_are_still_rejected(sample_git_repo):
    """放行的只是"代码那一格"，别的 token 一律照旧。"""
    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(
            ShellRequest("python", ["-c", "print(1)", "&&", "whoami"]),
            policy_workspace(sample_git_repo),
        )
    assert raised.value.error_type == "unsafe_argument"

    with pytest.raises(ShellPolicyError) as raised:
        ShellPolicy().validate(
            ShellRequest("python", ["-c", "print(1)"], cwd="a;b"),
            policy_workspace(sample_git_repo),
        )
    assert raised.value.error_type == "unsafe_argument"


def test_empty_inline_code_is_rejected(sample_git_repo):
    for arguments in (["-c"], ["-c", "   "]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("python", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type in {"unsafe_argument", "invalid_arguments"}


def test_uv_run_python_inline_code_is_allowed_too(sample_git_repo):
    """`uv run python -c` 和 `python -c` 是同一个能力，不该一个放行一个拒绝。"""
    validated = ShellPolicy().validate(
        ShellRequest("uv", ["run", "python", "-c", "print(1)"]),
        policy_workspace(sample_git_repo),
    )

    assert validated.program == "uv"
    assert validated.approval_required is True


def test_rejects_python_m_and_bare_flags(sample_git_repo):
    """只放行 -c；`-m` 和裸 flag 仍然拒绝，且消息要指向可用写法。"""
    for arguments in (["-m", "pytest"], ["-"], ["--version"]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("python", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type == "unsafe_argument"
        assert "python" in raised.value.public_message.lower()


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
    # `uv run python -c <code>` 已改为走审批，不再出现在这个拒绝清单里。
    for arguments in (["run", "powershell", "x"], ["run"], ["run", "python", "-m", "pytest"]):
        with pytest.raises(ShellPolicyError) as raised:
            ShellPolicy().validate(ShellRequest("uv", list(arguments)), policy_workspace(sample_git_repo))
        assert raised.value.error_type in {"subcommand_not_allowed", "unsafe_argument"}
