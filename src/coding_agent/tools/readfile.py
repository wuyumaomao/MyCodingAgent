from __future__ import annotations

from pathlib import Path
from typing import Any

from ..repository import Workspace, WorkspaceViolation


class ReadFileTool:
    name = "readfile"
    description = (
        "Read a UTF-8 text file inside the repository workspace. "
        "Optionally provide one-based inclusive start and end line numbers for a focused range."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative file path."},
            "start": {"type": "integer", "minimum": 1, "description": "First line to return (one-based)."},
            "end": {"type": "integer", "minimum": 1, "description": "Last line to return (inclusive, one-based)."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, max_bytes: int = 64 * 1024, max_file_bytes: int = 64 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.workspace = workspace
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes

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

        start = arguments.get("start", 1)
        end = arguments.get("end")
        if (
            not _valid_line_number(start)
            or ("end" in arguments and not _valid_line_number(end))
            or ("start" in arguments and end is not None and start > end)
        ):
            return _error("invalid_arguments", "Invalid readfile line range")

        try:
            size = target.stat().st_size
        except OSError:
            return _error("read_error", "File cannot be read")
        if size > self.max_file_bytes:
            return _error("file_too_large", "File is too large to read; use search to locate the relevant lines")

        try:
            kept, line_count, truncated = _read_lines(target, start, end, self.max_bytes)
        except PermissionError:
            return _error("permission_denied", "File cannot be read")
        except UnicodeDecodeError:
            return _error("decode_error", "File is not valid UTF-8 text")
        except OSError:
            return _error("read_error", "File cannot be read")

        if line_count == 0:
            if "start" in arguments or "end" in arguments:
                return _error("invalid_arguments", "Read range is empty")
            return {"ok": True, "path": path, "content": "", "start": 1, "end": 0, "line_count": 0, "truncated": False}
        if start > line_count:
            return _error("invalid_arguments", "Read range starts after the end of the file")
        if kept is None:
            return _error("file_too_large", "Requested content exceeds the read limit; narrow the range or use search")

        actual_end = start + len(kept) - 1
        numbered = "\n".join(
            f"{number}: {text}" for number, text in zip(range(start, actual_end + 1), kept)
        )
        return {
            "ok": True,
            "path": path,
            "content": numbered,
            "start": start,
            "end": actual_end,
            "line_count": line_count,
            "truncated": truncated,
        }


def _read_lines(target: Path, start: int, end: int | None, max_bytes: int) -> tuple[list[str] | None, int, bool]:
    """Stream the requested lines and bound the result, not the source file.

    Lines outside the requested range are counted but never kept, so reading a
    range from a large file stays memory bounded while still reporting an
    accurate ``line_count``. ``None`` is returned for the kept lines when not
    even the first requested line fits the quota.
    """
    kept: list[str] = []
    used = 0
    line_count = 0
    truncated = False
    with target.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line_count = number
            if number < start or (end is not None and number > end):
                continue
            text = raw[:-1] if raw.endswith("\n") else raw
            cost = len(f"{number}: {text}".encode("utf-8")) + (1 if kept else 0)
            if used + cost > max_bytes:
                if not kept:
                    return None, line_count, False
                truncated = True
                continue
            if not truncated:
                kept.append(text)
                used += cost
    return kept, line_count, truncated


def _valid_line_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
