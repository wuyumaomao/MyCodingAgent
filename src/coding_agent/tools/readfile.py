from __future__ import annotations

from pathlib import Path
from typing import Any

from ..repository import Workspace, WorkspaceViolation


class ReadFileTool:
    name = "readfile"
    description = (
        "Read a UTF-8 text file inside the repository workspace. "
        "Optionally provide one-based inclusive start and end line numbers for a focused range. "
        "The result ends with a footer telling you exactly which lines you received and the "
        "start value to continue from, so never guess the next range. "
        "A line longer than 2000 characters is truncated and marked."
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

    def __init__(
        self,
        workspace: Workspace,
        max_bytes: int = 64 * 1024,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_line_chars: int = 2000,
        max_lines: int = 2000,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if max_line_chars <= 0:
            raise ValueError("max_line_chars must be positive")
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        self.max_line_chars = max_line_chars
        self.max_lines = max_lines
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
        # 这个大文件上限只挡"整读"：给了范围就放行，因为 _read_lines 是流式的，
        # 范围外的行不会保留，内存上没有风险。原来不分情况一律拒绝，导致一个
        # 100 MB 的日志文件连第 1-5 行都读不出来。
        if size > self.max_file_bytes and "start" not in arguments and "end" not in arguments:
            return _error(
                "file_too_large",
                f"File is {size // (1024 * 1024)} MiB, too large to read whole; "
                "pass start/end to read a specific range, or use search to locate text",
            )

        try:
            kept, line_count, truncated = _read_lines(target, start, end, self.max_bytes, self.max_line_chars, self.max_lines)
        except PermissionError:
            return _error("permission_denied", "File cannot be read")
        except UnicodeDecodeError:
            return _error("decode_error", "File is not valid UTF-8 text")
        except OSError:
            return _error("read_error", "File cannot be read")

        if line_count == 0:
            if "start" in arguments or "end" in arguments:
                return _error("invalid_arguments", "Read range is empty")
            return {
                "ok": True,
                "path": path,
                "content": "",
                "start": 1,
                "end": 0,
                "line_count": 0,
                "truncated": False,
                "footer": "(End of file - this file is empty.)",
            }
        if start > line_count:
            return _error("invalid_arguments", "Read range starts after the end of the file")
        if kept is None:
            return _error("file_too_large", "A single line does not fit the read quota; use search to locate the relevant text")

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
            "footer": _footer(start, actual_end, line_count, truncated),
        }


def _footer(start: int, end: int, line_count: int, truncated: bool) -> str:
    """工具级的续读指令：自己说清下一步读哪一段。

    这一条**必须由工具返回**，不能指望压缩层补：压缩层只在视图越线时才修剪，而一个
    64 KiB 的截断结果远小于触发线，压根不会被修剪——模型那时只拿到一个裸的 `end`，
    得自己推 `start = end + 1`。三种形态对应三种状态（对齐 DSH 的 read footer）：
    被配额截断 / 还有后续 / 已到文件尾。
    """
    if truncated:
        return f"(Output capped: showing lines {start}-{end} of {line_count}. Use start={end + 1} to continue.)"
    if end >= line_count:
        return f"(End of file - total {line_count} lines.)"
    return f"(Showing lines {start}-{end} of {line_count}. Use start={end + 1} to read the next range.)"


def _read_lines(
    target: Path,
    start: int,
    end: int | None,
    max_bytes: int,
    max_line_chars: int,
    max_lines: int,
) -> tuple[list[str] | None, int, bool]:
    """Stream the requested lines and bound the result, not the source file.

    Lines outside the requested range are counted but never kept, so reading a
    range from a large file stays memory bounded while still reporting an
    accurate ``line_count``. The scan continues past the caps on purpose: the
    total line count is what the footer reports (``of <total>``), and it is what
    "did I read this file whole" is judged on.

    Two caps, whichever comes first: ``max_lines`` (2000, same as DSH's
    ``readLimit``) and ``max_bytes`` (the returned-content quota). ``max_lines``
    makes the window predictable — the model knows how much one call yields —
    which is what makes the footer's continuation point stable.

    A line longer than ``max_line_chars`` is **truncated** (and marked), not treated
    as a failure: minified JS and single-line JSON are one line tens of thousands of
    characters wide, and refusing to read them means the model cannot see the file
    structure at all. ``None`` is only returned when even one truncated line cannot
    fit the byte quota.
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
            if len(text) > max_line_chars:
                omitted = len(text) - max_line_chars
                text = f"{text[:max_line_chars]} ... (line truncated to {max_line_chars} chars, {omitted} omitted)"
            cost = len(f"{number}: {text}".encode("utf-8")) + (1 if kept else 0)
            if len(kept) >= max_lines or used + cost > max_bytes:
                if not kept:
                    return None, line_count, False
                truncated = True
                continue
            kept.append(text)
            used += cost
    return kept, line_count, truncated


def _valid_line_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
