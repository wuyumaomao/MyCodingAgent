from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .filesystem import FileEntry, scan_files
from .models import AssistantTurn, ToolCall
from .repository import Workspace


SYSTEM_PROMPT = (
    "You are a  coding agent. Use only the provided tools, "
    "stay inside the repository workspace, and explain findings based on evidence."
)

IMPORTANT_FILE_MAX_BYTES = 12 * 1024
_IMPORTANT_FILES = (
    "README.md",
    "pyproject.toml",
    "package.json",
    "AGENTS.md",
    ".env.example",
)
_ENTRYPOINT_NAMES = ("main.py", "app.py", "run.py", "cli.py")


class ConversationContext:
    """Build static system messages and maintain the per-run message history."""

    def __init__(
        self,
        workspace: Workspace,
        repository_context_builder: Callable[[Workspace], str] | None = None,
    ) -> None:
        if repository_context_builder is None:#在这里扫描整个仓库
            repository_context_builder = build_repository_context
        self.system_messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": repository_context_builder(workspace)},
        ]
        self.history: list[dict[str, Any]] = []

    def add_user_request(self, query: str) -> None:
        self.history.append({"role": "user", "content": query})

    def add_assistant_turn(self, turn: AssistantTurn) -> None:
        self.history.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ],
            }
        )

    def add_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.history.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    def messages(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.system_messages + self.history)


def build_repository_context(
    workspace: Workspace,
    max_depth: int = 4,
    max_entries: int = 200,
) -> str:
    entries, truncated = scan_files(
        workspace,
        max_depth=max_depth,
        max_entries=max_entries,
    )
    lines = [
        "[Repository Context]",
        "root: <workspace>",
        "files:",
    ]
    for entry in entries:
        if entry.kind == "directory":
            lines.append(f"- {entry.path} (directory)")
        else:
            size = "unknown" if entry.size is None else str(entry.size)
            lines.append(f"- {entry.path} (file, {size} bytes)")
    lines.append(f"truncated: {str(truncated).lower()}")
    lines.extend(_important_files_context(workspace, entries))
    lines.extend(_git_status_context(workspace))
    return "\n".join(lines)


def _important_files_context(workspace: Workspace, entries: list[FileEntry]) -> list[str]:
    paths = list(_IMPORTANT_FILES)
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name == "__main__.py"
        and entry.path not in paths
    )
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name in _ENTRYPOINT_NAMES
        and entry.path not in paths
    )

    lines = ["", "[Important Files]"]
    for path in paths:
        lines.append(f"### {path}")
        file_path = workspace.root / path
        if not file_path.is_file() or file_path.is_symlink():
            lines.append("status: missing")
            continue
        try:
            raw = file_path.read_bytes()
        except OSError:
            lines.append("status: unreadable")
            continue
        was_truncated = len(raw) > IMPORTANT_FILE_MAX_BYTES
        content = raw[:IMPORTANT_FILE_MAX_BYTES].decode("utf-8", errors="replace")
        lines.append("status: present")
        lines.append(f"truncated: {str(was_truncated).lower()}")
        lines.append("content:")
        lines.extend(content.splitlines())
    return lines


def _git_status_context(workspace: Workspace) -> list[str]:
    lines = ["", "[Git Status]"]
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        lines.append("status: unavailable")
        return lines
    status = result.stdout.strip()
    lines.append(status if status else "clean")
    return lines
