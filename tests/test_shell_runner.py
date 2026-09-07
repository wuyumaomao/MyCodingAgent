from __future__ import annotations

import subprocess
import sys
from io import BytesIO

from coding_agent.repository import Workspace
from coding_agent.tools.shell_policy import ValidatedShellRequest
from coding_agent.tools.shell_runner import WindowsProcessRunner


def request(program="python", args=None, cwd=".", timeout=1.0):
    return ValidatedShellRequest(program, args or ["script.py"], cwd, timeout, program)


class FakeProcess:
    pid = 1234
    returncode = 0

    def __init__(self, stdout=b"ok", stderr=b"", *, timeout=False):
        self.stdout_data = stdout
        self.stderr_data = stderr
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired(["python"], timeout)
        return self.returncode

    def communicate(self, timeout=None):
        raise AssertionError("runner must drain stdout and stderr incrementally")

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_runner_uses_argument_list_and_shell_false(sample_git_repo):
    calls = {}
    process = FakeProcess()

    def popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return process

    result = WindowsProcessRunner(popen_factory=popen).run(request(), Workspace(sample_git_repo), 1024)
    assert calls["command"] == [sys.executable, "script.py"]
    assert calls["kwargs"]["shell"] is False
    assert calls["kwargs"]["cwd"] == str(sample_git_repo)
    assert result.stdout == "ok"


def test_runner_captures_stdout_and_stderr(sample_git_repo):
    process = FakeProcess(b"out", b"err")
    result = WindowsProcessRunner(popen_factory=lambda *a, **k: process).run(
        request(), Workspace(sample_git_repo), 1024
    )
    assert result.exit_code == 0
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.timed_out is False


def test_runner_truncates_each_stream_without_deadlock(sample_git_repo):
    process = FakeProcess(b"123456", b"abcdef")
    result = WindowsProcessRunner(popen_factory=lambda *a, **k: process).run(
        request(), Workspace(sample_git_repo), 3
    )
    assert result.stdout == "123"
    assert result.stderr == "abc"
    assert result.output_truncated is True


def test_runner_terminates_process_tree_on_timeout(monkeypatch, sample_git_repo):
    process = FakeProcess(timeout=True)
    killed = []
    monkeypatch.setattr("coding_agent.tools.shell_runner.subprocess.run", lambda command, **kwargs: killed.append(command))
    result = WindowsProcessRunner(popen_factory=lambda *a, **k: process).run(
        request(timeout=0.01), Workspace(sample_git_repo), 1024
    )
    assert result.timed_out is True
    assert killed == [["taskkill", "/PID", "1234", "/T", "/F"]]
    assert process.killed is True


def test_runner_returns_nonzero_exit_code(sample_git_repo):
    process = FakeProcess()
    process.returncode = 2
    result = WindowsProcessRunner(popen_factory=lambda *a, **k: process).run(
        request(), Workspace(sample_git_repo), 1024
    )
    assert result.exit_code == 2


def test_runner_removes_sensitive_environment_variables(monkeypatch, sample_git_repo):
    captured = {}
    process = FakeProcess()

    def popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setenv("CODING_AGENT_API_KEY", "secret")
    monkeypatch.setenv("SAFE_VALUE", "ok")
    WindowsProcessRunner(popen_factory=popen).run(request(), Workspace(sample_git_repo), 1024)
    assert "CODING_AGENT_API_KEY" not in captured["env"]
    assert captured["env"]["SAFE_VALUE"] == "ok"
