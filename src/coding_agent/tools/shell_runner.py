from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from ..repository import Workspace
from .shell_policy import ValidatedShellRequest
from .target_environment import resolve_target_python, target_venv_bin, target_venv_dir


@dataclass(frozen=True)
class ShellRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    duration_ms: float


class ShellRunnerError(RuntimeError):
    """Raised when a permitted executable cannot be started."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.public_message = message


class WindowsProcessRunner:
    def __init__(
        self,
        *,
        popen_factory: Callable[..., Any] | None = None,
        taskkill_runner: Callable[..., Any] | None = None,
    ) -> None:
        self._popen = popen_factory or subprocess.Popen
        self._taskkill = taskkill_runner or subprocess.run

    def run(
        self,
        request: ValidatedShellRequest,
        workspace: Workspace,
        max_output_bytes: int,
    ) -> ShellRunResult:
        command = self._command(request, workspace)
        environment = _safe_environment(workspace)
        started = time.perf_counter()
        try:
            process = self._popen(
                command,
                cwd=str(workspace.root / request.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                env=environment,
            )
        except FileNotFoundError as exc:
            raise ShellRunnerError("executable_not_found", "The requested executable was not found") from exc
        except OSError as exc:
            raise ShellRunnerError("process_error", "The command could not be started") from exc

        timed_out = False
        stdout_buffer = _OutputBuffer(max_output_bytes)
        stderr_buffer = _OutputBuffer(max_output_bytes)
        stdout_thread = threading.Thread(target=_drain_stream, args=(process.stdout, stdout_buffer), daemon=True)
        stderr_thread = threading.Thread(target=_drain_stream, args=(process.stderr, stderr_buffer), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.wait(timeout=request.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_tree(process)
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
                process.wait()

        stdout_thread.join()
        stderr_thread.join()

        return ShellRunResult(
            exit_code=getattr(process, "returncode", None),
            stdout=stdout_buffer.text,
            stderr=stderr_buffer.text,
            timed_out=timed_out,
            output_truncated=stdout_buffer.truncated or stderr_buffer.truncated,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    @staticmethod
    def _command(request: ValidatedShellRequest, workspace: Workspace | None = None) -> list[str]:
        return WindowsProcessRunner._command_for_workspace(request, workspace)

    @staticmethod
    def _command_for_workspace(request: ValidatedShellRequest, workspace: Workspace | None) -> list[str]:
        if request.program == "python":
            executable = resolve_target_python(workspace) if workspace is not None else None
            return [str(executable or sys.executable), *request.args]
        if request.program == "pytest":
            executable = resolve_target_python(workspace) if workspace is not None else None
            return [str(executable or sys.executable), "-m", "pytest", *request.args]
        if request.program == "npm":
            executable = shutil.which("npm.cmd") or "npm.cmd"
            return [executable, *request.args]
        if request.program == "uv":
            executable = shutil.which("uv.exe") or shutil.which("uv") or ("uv.exe" if os.name == "nt" else "uv")
            return [executable, *request.args]
        executable = shutil.which("git.exe") or shutil.which("git") or "git.exe"
        return [executable, *request.args]

    def _terminate_tree(self, process: Any) -> None:
        pid = getattr(process, "pid", None)
        if pid is not None and os.name == "nt":
            try:
                self._taskkill(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass


def _safe_environment(workspace: Workspace | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"CODING_AGENT_API_KEY", "OPENAI_API_KEY"}
        and not any(marker in key.upper() for marker in ("AUTHORIZATION", "TOKEN", "SECRET"))
    }
    if workspace is not None:
        venv = target_venv_dir(workspace)
        venv_bin = target_venv_bin(workspace)
        if venv_bin is not None:
            environment["PATH"] = str(venv_bin) + os.pathsep + environment.get("PATH", "")
            environment["VIRTUAL_ENV"] = str(venv)
    return environment


class _OutputBuffer:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._value = bytearray()
        self.truncated = False

    @property
    def text(self) -> str:
        return bytes(self._value).decode("utf-8", errors="replace")

    def append(self, chunk: bytes) -> None:
        remaining = self._max_bytes - len(self._value)
        if remaining > 0:
            self._value.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True


def _drain_stream(stream: Any, buffer: _OutputBuffer) -> None:
    try:
        while chunk := stream.read(8192):
            buffer.append(chunk)
    finally:
        stream.close()
