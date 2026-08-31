from __future__ import annotations

from typing import Any

from ..filesystem import scan_files
from ..repository import Workspace, WorkspaceViolation


class ListFilesTool:
    name = "listfiles"
    description = "List files and directories inside the repository workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative directory."},
            "max_depth": {"type": "integer", "minimum": 0},
            "max_entries": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            path = arguments.get("path", ".")
            max_depth = arguments.get("max_depth", 4)
            max_entries = arguments.get("max_entries", 200)
            if not isinstance(path, str) or not isinstance(max_depth, int) or isinstance(max_depth, bool):
                raise ValueError("path and max_depth have invalid types")
            if not isinstance(max_entries, int) or isinstance(max_entries, bool):
                raise ValueError("max_entries has an invalid type")
            entries, truncated = scan_files(self.workspace, path, max_depth, max_entries)
        except WorkspaceViolation:
            return {"ok": False, "error": {"type": "workspace_violation", "message": "Path must stay inside the repository"}}
        except (ValueError, NotADirectoryError):
            return {"ok": False, "error": {"type": "invalid_arguments", "message": "Invalid listfiles arguments"}}
        return {
            "ok": True,
            "entries": [entry.__dict__ for entry in entries],
            "truncated": truncated,
        }
