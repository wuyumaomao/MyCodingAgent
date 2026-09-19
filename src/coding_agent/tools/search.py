from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..filesystem import INTERNAL_DIRS, _load_ignore_spec
from ..repository import Workspace, WorkspaceViolation


class SearchTool:
    """Search UTF-8 text content inside the repository workspace."""

    name = "search"
    description = (
        "Recursively search text content under a repository path. "
        "Use this to locate a definition or a call site before reading, and read one medium range "
        "around the hits rather than one tiny range per hit. "
        "Prefer the smallest known directory (for example src or tests); use the repository root only "
        "when the file location is unknown. A file path is also accepted and searches just that file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
        "path": {
                "type": "string",
                "description": "Repository-relative path to search: a directory (searched recursively) or a single file. Prefer a narrow known path, and use '.' only when the location is unknown. Defaults to '.'.",
            },
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
            return _error("invalid_arguments", "Invalid search arguments")
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
            return _error("invalid_arguments", "Invalid search arguments")
        try:
            base = self.workspace.resolve_relative(path)
        except WorkspaceViolation:
            return _error("workspace_violation", "Path must stay inside the repository")
        # 模型经常把已知文件直接当 path 传进来。报错只会白费一轮，直接搜那个文件。
        if not base.exists():
            return _error("path_not_found", "Search path does not exist")
        if not base.is_dir() and not base.is_file():
            return _error("not_a_directory", "Search path is neither a file nor a directory")

        try:
            re.compile(pattern)
        except re.error:
            return _error("invalid_arguments", "Pattern is not a valid regular expression")

        rg = shutil.which("rg")
        # For a single file, use the Python path directly.  ripgrep applies
        # repository/global ignore rules even when a newly-created file is
        # passed explicitly, which can make a known file appear to have no
        # matches.  The Python iterator already handles the exact-file case.
        if rg and base.is_dir():
            return _search_with_rg(rg, self.workspace, base, pattern, max_results)
        return _search_with_python(self.workspace, base, pattern, max_results)


def _search_with_rg(rg: str, workspace: Workspace, base: Path, pattern: str, max_results: int) -> dict[str, Any]:
    relative_base = base.relative_to(workspace.root).as_posix() or "."
    command = [
        rg,
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--glob",
        "!.git/**",
        "--glob",
        "!.codex/**",
        "--glob",
        "!.venv/**",
        "--glob",
        "!.coding-agent/**",
        "--glob",
        "!.pytest_cache/**",
        "--glob",
        "!dist/**",
        "--glob",
        "!build/**",
        "--glob",
        "!node_modules/**",
        pattern,
        relative_base,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return _search_with_python(workspace, base, pattern, max_results)
    if completed.returncode not in (0, 1):
        return _error("search_error", "Search could not be completed")
    matches = []
    for line in completed.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        matches.append({"path": Path(parts[0]).as_posix(), "line": int(parts[1]), "text": parts[2]})
        if len(matches) >= max_results:
            break
    return {"ok": True, "matches": matches, "truncated": len(matches) < len(completed.stdout.splitlines())}


def _search_with_python(workspace: Workspace, base: Path, pattern: str, max_results: int) -> dict[str, Any]:
    compiled = re.compile(pattern)
    ignore_spec = _load_ignore_spec(workspace.root)
    matches: list[dict[str, Any]] = []
    truncated = False
    for path in _iter_files(workspace, base, ignore_spec):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(workspace.root).as_posix()
        for number, text in enumerate(lines, 1):
            if compiled.search(text):
                if len(matches) >= max_results:
                    truncated = True
                    break
                matches.append({"path": relative, "line": number, "text": text})
        if truncated:
            break
    return {"ok": True, "matches": matches, "truncated": truncated}


def _iter_files(workspace: Workspace, base: Path, ignore_spec: Any):
    candidates = [base] if base.is_file() else sorted(base.rglob("*"), key=lambda item: item.relative_to(workspace.root).as_posix())
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace.root)
        if any(part in INTERNAL_DIRS for part in relative.parts):
            continue
        if ignore_spec.match_file(relative.as_posix()):
            continue
        yield path


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
