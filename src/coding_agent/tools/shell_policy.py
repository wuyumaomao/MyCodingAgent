from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from ..repository import Workspace, WorkspaceViolation


_ALLOWED_PROGRAMS = frozenset({"python", "pytest", "git", "npm", "uv"})
_READ_ONLY_GIT_COMMANDS = frozenset({"status", "diff", "log", "show", "branch", "rev-parse"})
_UNSAFE_TOKENS = frozenset({"&", "|", ";", "<", ">", "`", "\r", "\n"})
_NESTED_SHELL_NAMES = frozenset({"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"})


class ShellPolicyError(ValueError):
    """A command rejected before it can reach a process runner."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.public_message = message


@dataclass(frozen=True)
class ShellRequest:
    program: str
    args: list[str]
    cwd: str = "."
    timeout: float | None = None


@dataclass(frozen=True)
class ValidatedShellRequest:
    program: str
    args: list[str]
    cwd: str
    timeout: float
    executable: str
    approval_required: bool = True

#定义 Shell 请求、策略和稳定错误，工具内部业务检验
class ShellPolicy:
    allowed_programs: frozenset[str] = _ALLOWED_PROGRAMS
    default_timeout: float = 60.0
    max_timeout: float = 300.0
    max_output_bytes: int = 64 * 1024

    def __init__(
        self,
        *,
        default_timeout: float = 60.0,
        max_timeout: float = 300.0,
        max_output_bytes: int = 64 * 1024,
    ) -> None:
        if default_timeout <= 0 or default_timeout > max_timeout:
            raise ValueError("default_timeout must be between 0 and max_timeout")
        if max_timeout <= 0 or max_output_bytes <= 0:
            raise ValueError("Shell limits must be positive")
        self.default_timeout = float(default_timeout)
        self.max_timeout = float(max_timeout)
        self.max_output_bytes = max_output_bytes

    def validate(self, request: ShellRequest, workspace: Workspace) -> ValidatedShellRequest:
        if not isinstance(request, ShellRequest):
            raise ShellPolicyError("invalid_arguments", "Invalid shell arguments")
        if not isinstance(request.program, str) or not request.program.strip():
            raise ShellPolicyError("invalid_arguments", "Program must be a non-empty string")
        if not isinstance(request.args, list) or not all(isinstance(item, str) for item in request.args):
            raise ShellPolicyError("invalid_arguments", "Arguments must be a list of strings")
        program = request.program.strip().lower()
        if program not in self.allowed_programs:
            raise ShellPolicyError("command_not_allowed", "The requested command is not allowed")
        tokens = [program, *request.args, request.cwd]
        if any(token in _UNSAFE_TOKENS or any(marker in token for marker in _UNSAFE_TOKENS) for token in tokens):
            raise ShellPolicyError("unsafe_argument", "Command contains an unsafe shell character")
        if any(Path(token).name.lower() in _NESTED_SHELL_NAMES for token in tokens if token):
            raise ShellPolicyError("unsafe_argument", "Nested shells are not allowed")

        timeout = self.default_timeout if request.timeout is None else request.timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > self.max_timeout:
            raise ShellPolicyError("invalid_arguments", "Timeout must be between 0 and 300 seconds")

        cwd = self._workspace_path(workspace, request.cwd, "cwd_not_found", must_exist=True, must_be_dir=True)
        self._validate_program_args(program, request.args, workspace)
        return ValidatedShellRequest(
            program=program,
            args=list(request.args),
            cwd=_relative_display(workspace.root, cwd),
            timeout=float(timeout),
            executable=program,
            approval_required=not (program == "git" and request.args and request.args[0].lower() in _READ_ONLY_GIT_COMMANDS),
        )

    def _validate_program_args(self, program: str, args: list[str], workspace: Workspace) -> None:
        if program == "python":
            if not args or args[0].startswith("-") or not args[0].lower().endswith(".py"):
                raise ShellPolicyError("unsafe_argument", "python may only execute a workspace .py script")
            self._workspace_path(workspace, args[0], "script_not_found", must_exist=True, must_be_file=True)
            return
        if program == "pytest":
            for token in args:
                if token.startswith("-"):
                    continue
                if _looks_like_path(token):
                    self._workspace_path(workspace, token, "workspace_violation", must_exist=False)
            return
        if program == "git":
            if not args or args[0].startswith("-") or args[0].lower() not in _READ_ONLY_GIT_COMMANDS:
                raise ShellPolicyError("subcommand_not_allowed", "The requested git subcommand is not allowed")
            for token in args[1:]:
                if _looks_like_path(token):
                    self._workspace_path(workspace, token, "workspace_violation", must_exist=False)
            return
        if program == "npm":
            if not args or args[0].lower() not in {"test", "run"}:
                raise ShellPolicyError("subcommand_not_allowed", "The requested npm subcommand is not allowed")
            if args[0].lower() == "run" and (len(args) != 2 or not args[1] or args[1].startswith("-")):
                raise ShellPolicyError("invalid_arguments", "npm run requires one script name")
            return
        if program == "uv":
            if args == ["sync", "--dev"]:
                return
            if len(args) >= 2 and args[0] == "run":
                if args[1] == "pytest":
                    self._validate_pytest_args(args[2:], workspace)
                    return
                if args[1] == "python" and len(args) >= 3:
                    script = args[2]
                    if script.startswith("-") or not script.lower().endswith(".py"):
                        raise ShellPolicyError("subcommand_not_allowed", "uv run python may only execute a workspace Python script")
                    self._workspace_path(workspace, script, "script_not_found", must_exist=True, must_be_file=True)
                    return
                script = args[1]
                if script.startswith("-") or not script.lower().endswith(".py"):
                    raise ShellPolicyError("subcommand_not_allowed", "uv run may only execute a workspace Python script")
                self._workspace_path(workspace, script, "script_not_found", must_exist=True, must_be_file=True)
                return
            raise ShellPolicyError("subcommand_not_allowed", "Only 'uv sync --dev' or 'uv run <script.py>' is allowed")

    def _validate_pytest_args(self, args: list[str], workspace: Workspace) -> None:
        for token in args:
            if token.startswith("-"):
                continue
            if _looks_like_path(token):
                self._workspace_path(workspace, token, "workspace_violation", must_exist=False)

    @staticmethod
    def _workspace_path(
        workspace: Workspace,
        value: str,
        missing_type: str,
        *,
        must_exist: bool,
        must_be_dir: bool = False,
        must_be_file: bool = False,
    ) -> Path:
        try:
            target = workspace.resolve_relative(value)
        except WorkspaceViolation as exc:
            raise ShellPolicyError("workspace_violation", "Path must stay inside the repository") from exc
        if _has_symlink_component(target, workspace.root):
            raise ShellPolicyError("workspace_violation", "Symbolic links are not allowed")
        if must_exist and not target.exists():
            raise ShellPolicyError(missing_type, "Requested path does not exist")
        if must_be_dir and target.exists() and not target.is_dir():
            raise ShellPolicyError("cwd_not_found", "Working directory is not a directory")
        if must_be_file and target.exists() and not target.is_file():
            raise ShellPolicyError("script_not_found", "Python script is not a file")
        return target


def _looks_like_path(value: str) -> bool:
    return value == "." or value == ".." or "/" in value or "\\" in value or value.lower().endswith((".py", ".js", ".ts"))


def _relative_display(root: Path, target: Path) -> str:
    relative = target.relative_to(root).as_posix()
    return relative or "."


def _has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False
