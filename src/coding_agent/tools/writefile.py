from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..repository import Workspace, WorkspaceViolation
from .approval import WriteApprovalGate, WritePreview
from .atomic_writer import AtomicWriter, WriteError


class WriteFileTool:
    name = "write_file"
    description = "Create or overwrite a UTF-8 text file inside the repository workspace after approval."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative or absolute file path."},
            "content": {"type": "string", "description": "Complete UTF-8 text content to write."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        approval_gate: WriteApprovalGate | None = None,
        max_bytes: int = 64 * 1024,
        writer: AtomicWriter | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.workspace = workspace
        self.max_bytes = max_bytes
        self.approval_gate = approval_gate or WriteApprovalGate()
        self.writer = writer or AtomicWriter()

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _error("invalid_arguments", "Invalid write_file arguments")
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str) or not path or not isinstance(content, str):
            return _error("invalid_arguments", "Invalid write_file arguments")
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            return _error("invalid_arguments", "Content must be valid UTF-8 text")
        if content_bytes > self.max_bytes:
            return _error("file_too_large", "File exceeds the write limit")

        try:
            lexical_target = _lexical_path(self.workspace, path)
            target = self.workspace.resolve_relative(path)
            if _has_symlink_component(lexical_target, self.workspace.root):
                return _error("workspace_violation", "Symbolic links are not allowed")
        except WorkspaceViolation:
            return _error("workspace_violation", "Path must stay inside the repository")
        except OSError:
            return _error("permission_denied", "Path cannot be inspected")

        try:
            existed = target.exists()
            if existed and target.is_symlink():
                return _error("workspace_violation", "Symbolic links are not allowed")
            if existed and not target.is_file():
                return _error("permission_denied", "Target is not a regular file")
            missing_directories = _missing_parents(self.workspace, target.parent)
        except OSError:
            return _error("permission_denied", "Target cannot be inspected")

        operation = "overwrite" if existed else "create"
        preview = WritePreview(
            operation=operation,
            path=_display_path(self.workspace, target),
            content=content,
            existed=existed,
            creates_directories=missing_directories,
        )
        decision = self.approval_gate.approve(preview)
        if decision == "required":
            return _error("approval_required", "User approval is required before writing")
        if decision == "denied":
            return _error("approval_denied", "User denied the file write")

        try:
            # 父目录不存在时自己建，而不是回一句"父目录不存在"让模型去绕路。
            # 真实 run 里那条错误让模型花了 6 轮：两次 `python -c os.makedirs` 被
            # shell 策略拒，然后写临时脚本、跑它、才写成文件。审批门已经把这个
            # 副作用（creates_directories）显示给用户了，所以这里是经过同意的。
            if missing_directories:
                target.parent.mkdir(parents=True, exist_ok=True)
            bytes_written = self.writer.write(target, content)
        except PermissionError:
            return _error("permission_denied", "File cannot be written")
        except OSError:
            return _error("permission_denied", "Parent directory cannot be created")
        except WriteError:
            return _error("write_error", "File could not be written")
        result = {
            "ok": True,
            "path": _display_path(self.workspace, target),
            "operation": operation,
            "bytes_written": bytes_written,
        }
        if missing_directories:
            result["created_directories"] = list(missing_directories)
        return result


def _lexical_path(workspace: Workspace, path: str) -> Path:
    raw = Path(path) if Path(path).is_absolute() else workspace.root / path
    return Path(os.path.abspath(raw))


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


def _missing_parents(workspace: Workspace, parent: Path) -> tuple[str, ...]:
    """Repository-relative paths of the directories that would have to be created.

    Returns them outermost-first so the approval preview reads like the order they
    will actually be created in (``docs``, then ``docs/notes``, …).
    """
    missing: list[str] = []
    current = parent
    while current != workspace.root and workspace.root in current.parents:
        if current.exists():
            break
        missing.append(_display_path(workspace, current))
        current = current.parent
    missing.reverse()
    return tuple(missing)


def _display_path(workspace: Workspace, target: Path) -> str:
    return target.relative_to(workspace.root).as_posix()


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
