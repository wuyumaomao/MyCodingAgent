from __future__ import annotations

from .filesystem import scan_files
from .repository import Workspace


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
    return "\n".join(lines)
