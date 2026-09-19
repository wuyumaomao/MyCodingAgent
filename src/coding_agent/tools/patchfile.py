from __future__ import annotations

from pathlib import Path
from typing import Any

from ..repository import Workspace, WorkspaceViolation
from .approval import WriteApprovalGate, WritePreview
from .atomic_writer import AtomicWriter, WriteError
from .writefile import _display_path, _has_symlink_component, _lexical_path


class PatchFileTool:
    name = "patch_file"
    description = "Replace exactly one occurrence of text in a UTF-8 file inside the repository workspace after approval."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative or absolute file path."},
            "old_text": {"type": "string", "description": "Exact text to replace once."},
            "new_text": {"type": "string", "description": "Replacement UTF-8 text."},
        },
        "required": ["path", "old_text", "new_text"],
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
            return _error("invalid_arguments", "Invalid patch_file arguments")
        path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not all(isinstance(value, str) for value in (path, old_text, new_text)) or not path:
            return _error("invalid_arguments", "Invalid patch_file arguments")

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
            if not target.exists():
                return _error("file_not_found", "File not found")
            if target.is_symlink():
                return _error("workspace_violation", "Symbolic links are not allowed")
            if not target.is_file():
                return _error("not_a_file", "Path is not a regular file")
            data = target.read_bytes()
        except PermissionError:
            return _error("permission_denied", "File cannot be read")
        except OSError:
            return _error("permission_denied", "File cannot be read")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return _error("decode_error", "File is not valid UTF-8 text")

        # 匹配要用**和 readfile 看到的同一种表示**。readfile 走文本模式（通用换行，
        # CRLF 显示成 LF），而这里读的是原始字节，两者不一致时模型照着 readfile 写的
        # `old_text="TODO\n"` 会在 CRLF 文件上永远匹配不上（真实 run 里模型试了两次
        # 才摸到"只传单行不带换行"这个 workaround）。
        line_ending = "\r\n" if "\r\n" in content else "\n"
        normalized = content.replace("\r\n", "\n")
        matches = normalized.count(old_text)
        if matches == 0:
            return _error("text_not_found", "The old text was not found")
        if matches != 1:
            return _error("text_not_unique", "The old text must match exactly once")
        updated = normalized.replace(old_text, new_text, 1)
        # 写回时恢复文件原来的换行风格，不要让 patch 顺手把 LF 改成 CRLF（或反过来）。
        if line_ending == "\r\n":
            updated = updated.replace("\n", "\r\n")
        bytes_written = len(updated.encode("utf-8"))
        if bytes_written > self.max_bytes:
            return _error("file_too_large", "Patched file exceeds the write limit")

        preview = WritePreview(
            operation="patch",
            path=_display_path(self.workspace, target),
            old_text=old_text,
            new_text=new_text,
            existed=True,
        )
        decision = self.approval_gate.approve(preview)
        if decision == "required":
            return _error("approval_required", "User approval is required before writing")
        if decision == "denied":
            return _error("approval_denied", "User denied the file patch")

        try:
            self.writer.write(target, updated)
        except PermissionError:
            return _error("permission_denied", "File cannot be written")
        except WriteError:
            return _error("write_error", "File could not be written")
        return {
            "ok": True,
            "path": _display_path(self.workspace, target),
            "operation": "patch",
            "replacements": 1,
            "bytes_written": bytes_written,
        }


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
