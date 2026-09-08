from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from ..filesystem import _load_ignore_spec
from ..repository import Workspace, WorkspaceViolation


class FindFilesTool:
    """Find files by name or glob pattern inside the repository workspace."""

    name = "find_files"
    description = "Find workspace files by filename or glob pattern."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Filename or glob pattern."},
            "path": {"type": "string", "description": "Repository-relative directory to search."},
            "max_results": {"type": "integer", "minimum": 1},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, max_results: int = 200) -> None:
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        self.workspace = workspace
        self.max_results = max_results

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _error("invalid_arguments", "Invalid find_files arguments")
        pattern = arguments.get("pattern")
        path = arguments.get("path", ".")
        max_results = arguments.get("max_results", self.max_results)
        if (
            not isinstance(pattern, str)
            or not pattern
            or not isinstance(path, str)
            or not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or max_results <= 0
        ):
            return _error("invalid_arguments", "Invalid find_files arguments")
        try:
            base = self.workspace.resolve_relative(path)
        except WorkspaceViolation:
            return _error("workspace_violation", "Path must stay inside the repository")
        if not base.is_dir():
            return _error("not_a_directory", "Find path is not a directory")

        ignore_spec = _load_ignore_spec(self.workspace.root)
        files: list[str] = []
        truncated = False
        for candidate in sorted(base.rglob("*"), key=lambda item: item.relative_to(self.workspace.root).as_posix()):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(self.workspace.root)
            if ".git" in relative.parts or any(part in {".codex", ".venv", "node_modules"} for part in relative.parts):
                continue
            if ignore_spec.match_file(relative.as_posix()):
                continue
            relative_text = relative.as_posix()
            if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(relative_text, pattern):
                if len(files) >= max_results:
                    truncated = True
                    break
                files.append(relative_text)
        return {"ok": True, "files": files, "truncated": truncated}


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
