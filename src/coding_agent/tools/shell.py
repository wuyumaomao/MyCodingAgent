from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..repository import Workspace
from .approval import emit_current_event
from .shell_policy import ShellPolicy, ShellPolicyError, ShellRequest
from .shell_runner import ShellRunResult, ShellRunnerError, WindowsProcessRunner


ApprovalDecision = Literal["approved", "denied", "required", "auto_approved"]


@dataclass(frozen=True)
class ShellPreview:
    program: str
    args: list[str]
    cwd: str
    timeout: float

    @property
    def command(self) -> str:
        return " ".join([self.program, *self.args])

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["command"] = self.command
        return result


class ShellApprovalGate:
    def __init__(self, ask: Callable[[ShellPreview], bool] | None = None) -> None:
        self._ask = ask

    def approve(self, preview: ShellPreview) -> ApprovalDecision:
        emit_current_event(
            "approval_request",
            operation="shell",
            program=preview.program,
            cwd=preview.cwd,
            report_payload={"preview": preview.as_dict()},
        )
        if self._ask is None:
            decision: ApprovalDecision = "required"
        else:
            try:
                decision = "approved" if self._ask(preview) else "denied"
            except Exception:
                decision = "required"
        emit_current_event(
            "approval_result",
            operation="shell",
            program=preview.program,
            decision=decision,
        )
        return decision


class ShellTool:
    name = "shell"
    description = "Run an approved, allowlisted command inside the repository workspace."
    parameters = {
        "type": "object",
        "properties": {
            "program": {"type": "string", "enum": ["python", "pytest", "git", "npm", "uv"]},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
        },
        "required": ["program", "args"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        policy: ShellPolicy | None = None,
        approval_gate: ShellApprovalGate | None = None,
        runner: WindowsProcessRunner | None = None,
    ) -> None:
        self.workspace = workspace
        self.policy = policy or ShellPolicy()
        self.approval_gate = approval_gate or ShellApprovalGate()
        self.runner = runner or WindowsProcessRunner()

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:#把模型传来的 JSON 参数转换成 ShellRequest
            request = _request_from_arguments(arguments)
            #policy做验证，workspace逃逸，参数是否正确，白名单
            validated = self.policy.validate(request, self.workspace)
        except ShellPolicyError as exc:
            return _error(exc.error_type, exc.public_message)

        preview = ShellPreview(validated.program, validated.args, validated.cwd, validated.timeout)
        if validated.approval_required:
            decision = self.approval_gate.approve(preview)#询问是否通过，记录在trace
        else:
            decision = "auto_approved"
            emit_current_event(
                "approval_result",
                operation="shell",
                program=preview.program,
                decision=decision,
                reason="read_only_command",
            )
        if decision == "required":
            return _error("approval_required", "User approval is required before running a command")
        if decision == "denied":
            return _error("approval_denied", "User denied the command")

        try:
            result = self.runner.run(validated, self.workspace, self.policy.max_output_bytes)
        except ShellRunnerError as exc:
            return _error(exc.error_type, exc.public_message)
        return _result(validated.program, validated.args, validated.cwd, result)


def _request_from_arguments(arguments: dict[str, Any]) -> ShellRequest:
    if not isinstance(arguments, dict):
        raise ShellPolicyError("invalid_arguments", "Invalid shell arguments")
    return ShellRequest(
        program=arguments.get("program"),
        args=arguments.get("args"),
        cwd=arguments.get("cwd", "."),
        timeout=arguments.get("timeout"),
    )


def _result(program: str, args: list[str], cwd: str, result: ShellRunResult) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": not result.timed_out and result.exit_code == 0,
        "program": program,
        "args": args,
        "cwd": cwd,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "output_truncated": result.output_truncated,
        "duration_ms": result.duration_ms,
    }
    if result.timed_out:
        response["error"] = {"type": "timeout", "message": "The command timed out"}
    elif result.exit_code != 0:
        response["error"] = {"type": "process_error", "message": "The command exited with a non-zero status"}
    return response


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
