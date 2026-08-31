from __future__ import annotations

from typing import Any

from ..repository import Workspace, WorkspaceViolation


class ReadFileTool:
    name = "readfile"
    description = "Read a UTF-8 text file inside the repository workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative file path."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, max_bytes: int = 64 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.workspace = workspace
        self.max_bytes = max_bytes

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _error("invalid_arguments", "Invalid readfile arguments")
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return _error("invalid_arguments", "Invalid readfile arguments")
        try:
            target = self.workspace.resolve_relative(path)
        except WorkspaceViolation:
            return _error("workspace_violation", "Path must stay inside the repository")
        if not target.exists():
            return _error("file_not_found", "File not found")
        if not target.is_file():
            return _error("not_a_file", "Path is not a file")
        try:
            data = target.read_bytes()
        except PermissionError:
            return _error("permission_denied", "File cannot be read")
        except OSError:
            return _error("read_error", "File cannot be read")
        if len(data) > self.max_bytes:
            return _error("file_too_large", "File exceeds the read limit")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return _error("decode_error", "File is not valid UTF-8 text")
        return {"ok": True, "path": path, "content": content}


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
